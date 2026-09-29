"""Command-line entry point.

Three flags exist because they remove a class of setup mistake rather than because they are
convenient:

  * `--list-devices` answers "what exactly do I pass to `--device`?" from the same enumeration
    the server uses, instead of the user guessing at a name.
  * `--print-config` emits the host snippet with the interpreter that is actually running this
    package, so a host cannot resolve a bare command name against its own PATH and pick the
    wrong Python.
  * `--backend simulator` makes it possible to try the server, and read its tool list, with no
    camera attached.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .calibration import STATE_DIR_ENV
from .server import configure_logging, main_server

BACKENDS = ("auto", "dshow", "simulator")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line surface."""
    parser = argparse.ArgumentParser(
        prog="uvc-ptz-mcp",
        description=(
            "MCP server for USB (UVC) pan/tilt/zoom cameras. Every move is confirmed by "
            "comparing the picture before and after, because this class of camera reports "
            "positions it never moved to."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=BACKENDS,
        default="auto",
        help="auto (default) prefers a real camera and falls back to the simulator, "
        "dshow forces the Windows DirectShow path, simulator never touches hardware",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="substring of the camera's name, e.g. osmo or 'cam link'. See --list-devices.",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help=f"where calibration is stored (default: ${STATE_DIR_ENV} or ~/.uvc-ptz-camera-mcp)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="list the video input devices this machine exposes, then exit",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="print the MCP host configuration snippet, then exit",
    )
    parser.add_argument("--version", action="store_true", help="print the version, then exit")
    return parser


def host_config(args: argparse.Namespace) -> dict:
    """Return the host snippet, reflecting the flags the user actually passed."""
    forwarded: list[str] = []
    if args.backend != "auto":
        forwarded += ["--backend", args.backend]
    if args.device:
        forwarded += ["--device", args.device]
    if args.state_dir:
        forwarded += ["--state-dir", args.state_dir]
    return {
        "mcpServers": {
            "uvc-ptz-camera-mcp": {
                "command": sys.executable,
                "args": ["-m", "uvc_ptz_mcp"] + forwarded,
            }
        }
    }


def list_devices(device_hint: str | None) -> int:
    """Print the video input devices, flagging which one a hint would select."""
    try:
        from .dshow import enumerate_video_devices  # noqa: PLC0415 - optional extra
    except Exception as error:  # noqa: BLE001 - the extras may not be installed
        print(f"cannot enumerate devices: {error}", file=sys.stderr)
        print(
            "install the DirectShow extras: pip install 'uvc-ptz-camera-mcp[dshow]'",
            file=sys.stderr,
        )
        return 1

    try:
        devices, notes = enumerate_video_devices()
    except Exception as error:  # noqa: BLE001
        print(f"cannot enumerate devices: {error}", file=sys.stderr)
        print(
            "if the DirectShow extras are installed, this is the environment refusing to load "
            "the device enumerator itself",
            file=sys.stderr,
        )
        return 1

    for note in notes:
        print(f"  (skipped: {note})", file=sys.stderr)

    if not devices:
        print("no usable video input devices found")
        return 1

    for index, name in enumerate(devices):
        marker = ""
        if device_hint and device_hint.lower() in name.lower():
            marker = "   <- matches --device"
        print(f"  [{index}] {name}{marker}")
    if device_hint and not any(device_hint.lower() in name.lower() for name in devices):
        print(f"\nno device matches {device_hint!r}; the server would fall back to the simulator")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and either print something or serve."""
    args = build_parser().parse_args(argv)

    if args.version:
        print(f"uvc-ptz-camera-mcp {__version__}")
        return 0

    if args.state_dir:
        os.environ[STATE_DIR_ENV] = args.state_dir

    if args.list_devices:
        return list_devices(args.device)

    if args.print_config:
        print(json.dumps(host_config(args), indent=2))
        return 0

    configure_logging()
    main_server(args.backend, args.device)
    return 0


if __name__ == "__main__":
    sys.exit(main())
