"""Following a subject, by closing the loop on the picture.

The device's own position report cannot be trusted (see `docs/measurements.md`), so a follow loop
cannot ask where the camera is pointing. It does the only thing that works: look at the frame,
measure how far the subject is from the centre, move a bounded amount toward it, and look again.

Why the loop is slow, and why that is not a defect to fix: on the reference camera a write takes
about 0.4 s to produce motion, a pan completes in about a second, and a 4x zoom takes about 2.5 s.
Nothing faster than a one- or two-hertz loop can honestly claim to be following, because the
actuator simply is not there yet. A faster loop would issue commands the hardware has not finished
answering.

Two signs and scales in here are *mounting*-dependent rather than device-dependent -- whether
increasing pan swings the view left or right, and how many degrees a pixel of offset is worth. Both
are explicit parameters rather than clever guesses, and the loop reports its residual error every
iteration so a wrong choice shows up as error that grows instead of shrinking.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

# A subject this far from the centre, in pixels, is close enough: below it the loop stops moving,
# because hunting for the last few pixels is how a camera ends up jittering forever.
DEFAULT_DEAD_ZONE_PX = 24

# One iteration moves at most this many degrees. A bounded step keeps a wrong gain from throwing
# the camera across its range in a single command, and the loop converges anyway because it
# re-measures after every move.
DEFAULT_MAX_STEP_DEGREES = 25.0

# Degrees of camera movement per pixel of subject offset. The reference device's pan range is 253
# degrees over a 320-pixel frame, which is where this default comes from; it is a starting point,
# not a measurement, and the loop's reported residual is how you tell whether it is close.
DEFAULT_GAIN_DEG_PER_PX = 0.79


@dataclass(frozen=True)
class Target:
    """Something the detector found, in frame pixels."""

    label: str
    x: float
    y: float
    width: float = 0.0
    height: float = 0.0
    confidence: float = 0.0

    def to_dict(self) -> dict:
        """Serialise for a tool result."""
        return {
            "label": self.label,
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "width": round(self.width, 1),
            "height": round(self.height, 1),
            "confidence": round(self.confidence, 3),
        }


@dataclass(frozen=True)
class Offset:
    """Where the target is relative to the centre of the frame, in pixels."""

    dx: float
    dy: float

    @property
    def distance(self) -> float:
        """How far off centre, in pixels."""
        return float((self.dx**2 + self.dy**2) ** 0.5)

    def to_dict(self) -> dict:
        """Serialise for a tool result."""
        return {
            "dx": round(self.dx, 1),
            "dy": round(self.dy, 1),
            "distance": round(self.distance, 1),
        }


class Detector(Protocol):
    """Anything that can find a subject in a greyscale frame."""

    def locate(self, frame: np.ndarray) -> Target | None:
        """Return the subject, or None when it is not in this frame."""


class FaceDetector:
    """Faces, using OpenCV's bundled cascade.

    Deliberately the Haar cascade rather than a downloaded model: it ships inside the OpenCV
    wheel, needs no network at first use, and runs in a few milliseconds on a CPU. That matters
    for a server that must work with no account, no cloud and no model download.
    """

    def __init__(self, scale_factor: float = 1.1, min_neighbours: int = 5) -> None:
        """Load the cascade, or explain what to install."""
        try:
            import cv2  # noqa: PLC0415 - an optional extra, imported only when following
        except ImportError as error:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                "following needs OpenCV: pip install 'uvc-ptz-camera-mcp[tracking]'"
            ) from error

        self._cv2 = cv2
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        self._cascade = cv2.CascadeClassifier(path)
        if self._cascade.empty():  # pragma: no cover - only if the wheel is broken
            raise RuntimeError(f"could not load the face cascade at {path}")
        self._scale_factor = scale_factor
        self._min_neighbours = min_neighbours

    def locate(self, frame: np.ndarray) -> Target | None:
        """Return the largest face in the frame, or None."""
        image = np.ascontiguousarray(frame.astype(np.uint8))
        faces = self._cascade.detectMultiScale(
            image, scaleFactor=self._scale_factor, minNeighbors=self._min_neighbours
        )
        if len(faces) == 0:
            return None
        # Largest first: with several faces in view, the nearest is the one worth following.
        x, y, width, height = max(faces, key=lambda box: int(box[2]) * int(box[3]))
        return Target(
            label="face",
            x=float(x) + float(width) / 2,
            y=float(y) + float(height) / 2,
            width=float(width),
            height=float(height),
        )


def offset_from(target: Target, frame: np.ndarray) -> Offset:
    """Measure the target's distance from the centre of the frame."""
    height, width = frame.shape[:2]
    return Offset(dx=target.x - width / 2, dy=target.y - height / 2)


def step_for(
    offset: Offset,
    *,
    gain: float = DEFAULT_GAIN_DEG_PER_PX,
    dead_zone_px: int = DEFAULT_DEAD_ZONE_PX,
    max_step: float = DEFAULT_MAX_STEP_DEGREES,
    invert: bool = False,
) -> tuple[float, float]:
    """Turn an offset into the degrees to move on (pan, tilt).

    Returns zero for an axis inside the dead zone, and clamps each axis to `max_step`. `invert`
    flips both signs, which is what a mount that answers the axes the other way round needs: the
    loop cannot know the convention, and a wrong one shows up immediately as error that grows.
    """
    pan = 0.0 if abs(offset.dx) <= dead_zone_px else offset.dx * gain
    tilt = 0.0 if abs(offset.dy) <= dead_zone_px else offset.dy * gain
    if invert:
        pan, tilt = -pan, -tilt
    return (
        float(max(-max_step, min(max_step, pan))),
        float(max(-max_step, min(max_step, tilt))),
    )


@dataclass
class Tracker:
    """The state of a follow loop, including the evidence for what it claims."""

    target: str = "face"
    gain: float = DEFAULT_GAIN_DEG_PER_PX
    dead_zone_px: int = DEFAULT_DEAD_ZONE_PX
    max_step: float = DEFAULT_MAX_STEP_DEGREES
    invert: bool = False
    interval: float = 0.5
    lost_after: int = 10
    max_seconds: float = 120.0
    max_failures: int = 5

    iterations: int = 0
    moves: int = 0
    move_failures: int = 0
    consecutive_failures: int = 0
    lost_frames: int = 0
    started_at: float = field(default_factory=time.monotonic)
    last_target: Target | None = None
    last_offset: Offset | None = None
    last_step: tuple[float, float] = (0.0, 0.0)
    history: list[dict] = field(default_factory=list)
    stopped_because: str | None = None
    running: bool = False

    def to_dict(self) -> dict:
        """Serialise for a tool result."""
        elapsed = time.monotonic() - self.started_at
        converged = self.last_offset is not None and self.last_offset.distance <= self.dead_zone_px
        return {
            "running": self.running,
            "target": self.target,
            "iterations": self.iterations,
            "moves": self.moves,
            "move_failures": self.move_failures,
            "consecutive_failures": self.consecutive_failures,
            "lost_frames": self.lost_frames,
            "elapsed_seconds": round(elapsed, 1),
            "converged": converged,
            "dead_zone_px": self.dead_zone_px,
            "gain_deg_per_px": self.gain,
            "inverted": self.invert,
            "last_target": None if self.last_target is None else self.last_target.to_dict(),
            "last_offset": None if self.last_offset is None else self.last_offset.to_dict(),
            "last_step_degrees": [round(value, 2) for value in self.last_step],
            "stopped_because": self.stopped_because,
            "history": self.history[-12:],
        }


async def _attempt_move(
    tracker: Tracker,
    move: Callable[[float, float], Awaitable[dict]],
    pan_step: float,
    tilt_step: float,
    entry: dict[str, Any],
) -> str | None:
    """Apply one iteration's step and record what happened. Returns a stop reason, or None.

    Both ways of failing are counted rather than thrown: a move that raised, and a move the camera
    accepted whose effect the picture never confirmed. The second is the more interesting one --
    it is this device's signature failure -- and a loop that stopped on the first hiccup would not
    be a loop, so the count has to reach `max_failures` before giving up.
    """
    try:
        report = await move(pan_step, tilt_step)
        confirmed = bool(report.get("moved", False))
        tracker.moves += 1
        entry["moved"] = confirmed
        entry["confirmed_by_picture"] = report.get("confirmed_by")
        if confirmed:
            tracker.consecutive_failures = 0
        else:
            tracker.consecutive_failures += 1
            entry["unconfirmed"] = True
    except Exception as error:  # noqa: BLE001 - a failed move is data, not a crash
        tracker.move_failures += 1
        tracker.consecutive_failures += 1
        entry["move_failed"] = str(error)[:200]

    if tracker.consecutive_failures >= tracker.max_failures:
        return f"{tracker.consecutive_failures} moves in a row did not move the camera"
    return None


async def run_loop(
    tracker: Tracker,
    *,
    detector: Detector,
    capture: Callable[[], Awaitable[np.ndarray]],
    move: Callable[[float, float], Awaitable[dict]],
    sleep: Callable[[float], Awaitable[None]],
    now: Callable[[], float] = time.monotonic,
) -> Tracker:
    """Follow the subject until it is lost, the clock runs out, or `tracker.running` goes false.

    Everything that touches the camera is injected: `capture` returns a frame, `move` applies an
    absolute pan/tilt delta and returns its own report. That is what lets the whole loop be tested
    against the simulator and a scripted detector, with no hardware and no model.
    """
    tracker.running = True
    while tracker.running:
        if now() - tracker.started_at >= tracker.max_seconds:
            tracker.stopped_because = f"reached max_seconds ({tracker.max_seconds:g})"
            break

        frame = await capture()
        found = detector.locate(frame)
        tracker.iterations += 1

        if found is None:
            tracker.lost_frames += 1
            tracker.history.append({"iteration": tracker.iterations, "found": False})
            if tracker.lost_frames >= tracker.lost_after:
                tracker.stopped_because = f"lost the target for {tracker.lost_frames} frames"
                break
            await sleep(tracker.interval)
            continue

        tracker.lost_frames = 0
        tracker.last_target = found
        offset = offset_from(found, frame)
        tracker.last_offset = offset
        pan_step, tilt_step = step_for(
            offset,
            gain=tracker.gain,
            dead_zone_px=tracker.dead_zone_px,
            max_step=tracker.max_step,
            invert=tracker.invert,
        )
        tracker.last_step = (pan_step, tilt_step)

        entry: dict[str, Any] = {
            "iteration": tracker.iterations,
            "found": True,
            "offset_px": round(offset.distance, 1),
            "step_degrees": [round(pan_step, 2), round(tilt_step, 2)],
        }

        if pan_step or tilt_step:
            reason = await _attempt_move(tracker, move, pan_step, tilt_step, entry)
            if reason is not None:
                tracker.stopped_because = reason
                tracker.history.append(entry)
                break
        else:
            # Inside the dead zone: settled, so stop asking. Reporting this as convergence rather
            # than as an idle loop is the difference between "it is holding" and "it is stuck".
            entry["settled"] = True
            tracker.history.append(entry)
            tracker.stopped_because = "the subject is inside the dead zone"
            break

        tracker.history.append(entry)
        await sleep(tracker.interval)

    tracker.running = False
    if tracker.stopped_because is None:
        tracker.stopped_because = "stopped on request"
    return tracker
