"""Layer 1b: the verdict function, tested against the calibration that produced its threshold.

Two things are asserted here. First that the metric behaves: identical frames score zero, a
shifted frame scores high, and a pure brightness change scores near zero -- that last one is the
property that makes the metric usable at all, because auto-exposure hunting otherwise looks
exactly like movement to a naive difference.

Second, that the shipped threshold still separates the labelled pairs measured on the reference
camera. If someone changes the metric or the threshold to something that no longer reproduces
that calibration, this fails rather than a tool quietly reporting the wrong verdict.

The calibration is kept here as **measurements, not pictures**: pair, label, and the value the
metric produced. The frames are not in this repository and must not be -- they are captures of a
room somebody lives in, and two of them showed a person. `test_no_captures_are_committed` enforces
that, because the wrong thing to do with a camera project is to publish somebody's home.
"""

from __future__ import annotations

import json
import pathlib

import numpy as np
import pytest

from uvc_ptz_mcp.verify import (
    DEFAULT_THRESHOLD,
    REFERENCE_HIGHEST_SAME,
    REFERENCE_LOWEST_CHANGED,
    compare,
    reference_cases,
    z_mad,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def textured(height: int = 180, width: int = 320, seed: int = 3) -> np.ndarray:
    """Build a deterministic, detailed frame, so shifts are measurable."""
    rng = np.random.default_rng(seed)
    frame = rng.normal(120.0, 25.0, size=(height, width))
    frame[:, ::20] -= 60  # strong vertical edges
    return np.clip(frame, 0, 255)


def shifted(frame: np.ndarray, pixels: int) -> np.ndarray:
    """Return the same scene as seen after the camera panned `pixels`."""
    return np.roll(frame, pixels, axis=1)


def test_identical_frames_score_zero():
    frame = textured()
    assert z_mad(frame, frame) == pytest.approx(0.0, abs=1e-6)


def test_a_shift_scores_far_above_the_threshold():
    frame = textured()
    assert z_mad(frame, shifted(frame, 40)) > DEFAULT_THRESHOLD


def test_a_brightness_change_alone_scores_below_the_threshold():
    """The false positive this metric exists to avoid: exposure drift, not movement."""
    frame = textured()
    brighter = np.clip(frame + 40.0, 0, 255)
    assert z_mad(frame, brighter) < 0.05
    assert compare(frame, brighter).changed is False


def test_noise_alone_scores_below_the_threshold():
    frame = textured()
    noisy = np.clip(frame + np.random.default_rng(11).normal(0, 2.0, frame.shape), 0, 255)
    assert compare(frame, noisy).changed is False


def test_compare_reports_the_numbers_it_used():
    frame = textured()
    verdict = compare(frame, shifted(frame, 60), detail="pan")
    assert verdict.changed is True
    payload = verdict.to_dict()
    assert payload["view_changed"] is True
    assert payload["threshold"] == DEFAULT_THRESHOLD
    assert payload["detail"] == "pan"


def test_empty_frames_are_refused_rather_than_scored():
    with pytest.raises(ValueError):
        z_mad(np.array([]), np.array([]))


def test_the_shipped_threshold_reproduces_the_hardware_calibration():
    """The provenance test: the threshold must still separate every recorded pair."""
    cases = reference_cases()
    same = [value for _, _, label, value in cases if label == "same"]
    changed = [value for _, _, label, value in cases if label == "changed"]

    assert same and changed, "the recorded calibration must contain both classes"
    assert max(same) == pytest.approx(REFERENCE_HIGHEST_SAME, abs=0.001)
    assert min(changed) == pytest.approx(REFERENCE_LOWEST_CHANGED, abs=0.001)
    assert max(same) < DEFAULT_THRESHOLD < min(changed), (
        "the shipped threshold no longer separates the labelled hardware pairs"
    )
    # and with real margin, not by a hair
    assert min(changed) / max(same) > 4.0


# --------------------------------------------------------------------------------------
# The calibration record on disk.
#
# What is kept is the measurement: which two frames, what the label is, and the value the metric
# produced. What is not kept is the pixels. To re-derive the numbers from images, point
# scripts/make_fixtures.py at a fresh capture session -- the method is preserved, the pictures are
# not, and that is the intended trade.


def recorded_calibration() -> dict:
    """Load the labelled pairs and their recorded values from the repository."""
    cases_file = FIXTURES / "verify_cases.json"
    assert cases_file.exists(), f"the calibration record is missing: {cases_file}"
    return json.loads(cases_file.read_text(encoding="utf-8"))


def test_the_recorded_calibration_and_the_code_hold_the_same_pairs():
    """Two copies of the same record exist; they must not drift apart.

    Pairs and labels must match exactly. Values are compared to the precision the code records
    them at, because that copy is written for reading: a difference beyond rounding would mean one
    of the two has been edited on its own.
    """
    recorded = recorded_calibration()
    from_disk = {(case["first"], case["second"]): case for case in recorded["cases"]}
    from_code = {
        (first, second): (label, value) for first, second, label, value in reference_cases()
    }

    assert set(from_disk) == set(from_code), "the record and the code list different pairs"
    for key, case in from_disk.items():
        label, value = from_code[key]
        assert case["label"] == label, f"{key} is labelled {case['label']} on disk, {label} in code"
        assert case["value"] == pytest.approx(value, abs=0.001), (
            f"{key}: {case['value']} on disk, {value} in code"
        )


def test_the_record_keeps_both_classes_and_their_separation():
    recorded = recorded_calibration()
    assert len(recorded["cases"]) >= 10, "the record should keep both classes well populated"
    assert {case["label"] for case in recorded["cases"]} == {"same", "changed"}
    assert recorded["highest_same"] < recorded["lowest_changed"]
    assert recorded["highest_same"] < DEFAULT_THRESHOLD < recorded["lowest_changed"]


# The three files below are the project's own mark, drawn by a script, not captured from a camera.
# They are the only images allowed in the tree: one is the Cline listing's logo, one is the bundle
# icon and one is its larger copy.
ALLOWED_IMAGES = {
    "assets/logo-400.png",
    "assets/logo-512.png",
    "mcpb/icon.png",
}
MEDIA_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
    ".heic",
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".webm",
    ".m4v",
    ".npz",
}
SKIP_PARTS = {".git", ".venv", ".venv310", "__pycache__", ".pytest_cache", ".ruff_cache", "dist"}


def test_no_captures_are_committed():
    """No image or video from a camera may be committed, anywhere.

    This is a guard rather than a formality. Frames captured during this project show a room
    somebody lives in, and two of them showed a person: they were packed into a fixture corpus
    that briefly went public before it was pulled. The only images that belong in this repository
    are the generated marks named above, so anything else fails here -- which is the check that
    would have stopped it the first time.
    """
    offenders = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if SKIP_PARTS & set(path.relative_to(REPO_ROOT).parts):
            continue
        if path.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in ALLOWED_IMAGES:
            continue
        offenders.append(relative)

    assert offenders == [], (
        "these carry camera captures and must not be committed: "
        f"{sorted(offenders)}. Generated marks belong in {sorted(ALLOWED_IMAGES)}."
    )
