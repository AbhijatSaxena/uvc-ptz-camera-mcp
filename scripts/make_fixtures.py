"""Rebuild the labelled fixture corpus from frames captured on the reference device.

The verification layer is the risky part of this server: deciding from pictures alone whether a
move happened, because the device's own position report cannot be trusted. That layer is testable
without hardware, but only against frames whose ground truth is known -- so the labelled corpus
behind `DEFAULT_THRESHOLD` is kept in the repository rather than in the scratch directory where
the measurements were taken.

The corpus is labelled by *what was observed at the time*, never by the metric: frames taken
during a 96-second run with no commands at all, and frames taken either side of a command that
demonstrably moved the camera. Recomputing the metric over those pairs is a regression test on
real hardware behaviour, and it is what makes the threshold a measurement rather than a guess.

Input: a directory of full-resolution captures, given with `--frames`.
Output: `verify_cases.json` (labels, notes, values -- the part that belongs in the repository) and
`frames.npz` (the frames at the size the metric uses: 320px wide, 8-bit greyscale). The frame pack
is gitignored and must stay out of the repository: these are captures of a room somebody lives in.
Measure with it, then keep the numbers.

Usage:
    python scripts/make_fixtures.py --frames <dir of captures> --check    # measure, write nothing
    python scripts/make_fixtures.py --frames <dir of captures>            # also write the pack
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from uvc_ptz_mcp.verify import DEFAULT_THRESHOLD, z_mad  # noqa: E402 - path set above

FRAME_WIDTH = 320
DEFAULT_OUT = ROOT / "tests" / "fixtures"

# (first, second, label, note). "same" means the camera was not commanded between the two frames
# or was commanded back to a pose it had not left; "changed" means a command demonstrably moved
# it, confirmed by a person looking at the two frames.
CASES: tuple[tuple[str, str, str, str], ...] = (
    ("q_00.png", "q_01.png", "same", "quiet baseline, consecutive"),
    ("q_00.png", "q_06.png", "same", "quiet baseline, 48s apart"),
    ("q_00.png", "q_11.png", "same", "quiet baseline, whole 96s run"),
    ("s_0.png", "s_60.png", "same", "Pan 0 vs Pan 60 commanded: the 60 write did not apply"),
    ("v2_0_0.png", "v2_1_60.png", "same", "Pan 0 vs 60, double-sent, 10s hold: no movement"),
    ("v2_0_0.png", "v2_2_0.png", "same", "Pan 0 vs 0 at the end of that run"),
    ("w_baseline.png", "w_2_pan-38.png", "changed", "observe_run: Pan -> -38"),
    ("w_baseline.png", "w_5_tilt-30.png", "changed", "observe_run: Tilt -> -30"),
    ("w_baseline.png", "w_6_zoom1200.png", "changed", "observe_run: Zoom -> 12x"),
    ("s_0.png", "s_0b.png", "changed", "the one unexplained view change in that run"),
)


def decode_grey(path: Path, width: int = FRAME_WIDTH) -> np.ndarray:
    """Decode one capture to a greyscale array at the width the metric uses.

    ffmpeg rather than an image library: decoding a PNG is the one thing it is needed for here,
    and the reference frames are full-resolution captures from a video device.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            f"scale={width}:-2",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        capture_output=True,
        timeout=120,
        check=True,
    )
    pixels = np.frombuffer(result.stdout, dtype=np.uint8)
    if pixels.size == 0:
        raise SystemExit(f"decoded no pixels from {path}")
    height = pixels.size // width
    return pixels[: height * width].reshape(height, width)


def main(argv: list[str] | None = None) -> int:
    """Build (or check) the corpus."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--frames",
        type=Path,
        required=True,
        help="directory of captures to measure; there is deliberately no default, because the "
        "original session directory no longer holds any",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where the corpus goes")
    parser.add_argument("--check", action="store_true", help="verify only; write nothing")
    args = parser.parse_args(argv)

    names = sorted({name for case in CASES for name in case[:2]})
    missing = [name for name in names if not (args.frames / name).exists()]
    if missing:
        print(f"missing {len(missing)} frames in {args.frames}: {missing[:4]}", file=sys.stderr)
        return 1

    frames = {name: decode_grey(args.frames / name) for name in names}
    values = [
        (first, second, label, note, z_mad(frames[first], frames[second]))
        for first, second, label, note in CASES
    ]

    high_same = max(value for first, second, label, note, value in values if label == "same")
    low_changed = min(value for first, second, label, note, value in values if label == "changed")
    separated = high_same < DEFAULT_THRESHOLD < low_changed

    print(f"{len(names)} frames decoded at {FRAME_WIDTH}px wide")
    for first, second, label, note, value in sorted(values, key=lambda row: row[4]):
        print(f"  {label:<8} {value:7.4f}  {note}")
    print(f"\n  highest 'same'    {high_same:.4f}")
    print(f"  lowest  'changed' {low_changed:.4f}")
    print(f"  threshold         {DEFAULT_THRESHOLD:.4f}  ({low_changed / high_same:.1f}x apart)")
    if not separated:
        print(
            "FAILED: the shipped threshold no longer separates the labelled pairs",
            file=sys.stderr,
        )
        return 1

    if args.check:
        print("\ncheck only: nothing written")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.out / "frames.npz", **frames)
    payload = {
        "source": "frames captured from a DJI Osmo Pocket 4P on 2026-09-26 via its UVC controls",
        "device": "DJI Osmo Pocket 4P (webcam mode), VID 0x2CA3 / PID 0x0023, since returned",
        "metric": "z-normalised mean absolute difference of 320px-wide greyscale frames",
        "labels": "assigned from what was observed at capture time, never from the metric",
        "frames_file": "frames.npz (one 8-bit greyscale array per name below)",
        "suggested_threshold": DEFAULT_THRESHOLD,
        "highest_same": round(high_same, 4),
        "lowest_changed": round(low_changed, 4),
        "cases": [
            {
                "first": first,
                "second": second,
                "label": label,
                "note": note,
                "value": round(value, 4),
            }
            for first, second, label, note, value in values
        ],
    }
    (args.out / "verify_cases.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\nwrote {args.out / 'frames.npz'} and {args.out / 'verify_cases.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
