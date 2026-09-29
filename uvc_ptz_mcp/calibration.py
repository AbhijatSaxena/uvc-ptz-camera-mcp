"""Per-device calibration: where the camera is pointing when it is pointed at *what*.

Pan is an absolute axis whose meaning is device- and mounting-specific. On the reference
camera, pan 0 faced a wall and pan 150-215 faced the operator -- measured by commanding a ladder
of values and looking at the result. Nothing about that transfers to another unit or even to the
same unit after it is moved, so it cannot live in code as a constant: it has to be learned, per
device, and stored.

The same file records the picture-difference threshold, so a user can tighten or loosen
verification for their own scene without editing the package.

Location: `$UVC_PTZ_STATE_DIR`, defaulting to `~/.uvc-ptz-camera-mcp/`. Nothing here is secret:
it holds angles and a threshold, never credentials.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .camera import Axis
from .verify import DEFAULT_THRESHOLD

STATE_DIR_ENV = "UVC_PTZ_STATE_DIR"


@dataclass
class Calibration:
    """What has been learned about one physical camera."""

    device: str = "unknown"
    neutral: dict[str, int] = field(default_factory=dict)
    aims: dict[str, dict[str, int]] = field(default_factory=dict)
    threshold: float = DEFAULT_THRESHOLD
    notes: list[str] = field(default_factory=list)

    def remember_aim(self, label: str, pose: dict[Axis, int]) -> None:
        """Record that the camera at `pose` is looking at whatever `label` names.

        Labels are the caller's words -- "me", "the desk", "the door". They are what makes a
        later instruction like "point at the desk" resolvable without a vision model.
        """
        clean = label.strip()
        if not clean:
            raise ValueError("an aim label cannot be empty")
        self.aims[clean] = {axis.value: int(value) for axis, value in pose.items()}

    def aim(self, label: str) -> dict[str, int]:
        """Return the pose recorded for `label`, or explain what is recorded."""
        if label not in self.aims:
            known = ", ".join(sorted(self.aims)) or "none yet"
            raise KeyError(f"no aim recorded for {label!r}; known aims: {known}")
        return dict(self.aims[label])

    def to_dict(self) -> dict:
        """Serialise for storage or a tool result."""
        payload = asdict(self)
        payload["neutral"] = {str(key): value for key, value in self.neutral.items()}
        return payload

    @classmethod
    def from_dict(cls, raw: dict) -> Calibration:
        """Rebuild from stored JSON, ignoring anything unrecognised."""
        return cls(
            device=str(raw.get("device", "unknown")),
            neutral={str(k): int(v) for k, v in (raw.get("neutral") or {}).items()},
            aims={
                str(label): {str(k): int(v) for k, v in (pose or {}).items()}
                for label, pose in (raw.get("aims") or {}).items()
            },
            threshold=float(raw.get("threshold", DEFAULT_THRESHOLD)),
            notes=[str(note) for note in (raw.get("notes") or [])],
        )


def state_dir() -> Path:
    """Where calibration lives: `$UVC_PTZ_STATE_DIR` or `~/.uvc-ptz-camera-mcp`."""
    configured = os.environ.get(STATE_DIR_ENV)
    base = Path(configured).expanduser() if configured else Path.home() / ".uvc-ptz-camera-mcp"
    return base


def calibration_path(device: str) -> Path:
    """Return the calibration file for one device, named after that device."""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in device)
    safe = safe or "default"
    return state_dir() / f"{safe}.json"


def load_calibration(device: str) -> Calibration:
    """Read a device's calibration, or start a fresh one. Never raises for a missing file."""
    path = calibration_path(device)
    if not path.exists():
        return Calibration(device=device)
    try:
        return Calibration.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError, OSError) as error:
        # A corrupt calibration must not stop the server: start clean and say so.
        fresh = Calibration(device=device)
        fresh.notes.append(
            f"existing calibration unreadable ({type(error).__name__}); started fresh"
        )
        return fresh


def save_calibration(calibration: Calibration) -> Path:
    """Write a device's calibration, creating the directory if needed."""
    path = calibration_path(calibration.device)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(calibration.to_dict(), indent=2), encoding="utf-8")
    return path
