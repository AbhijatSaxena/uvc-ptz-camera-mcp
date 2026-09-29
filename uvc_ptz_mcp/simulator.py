"""A simulated PTZ camera, calibrated from measurements of the reference device.

This exists so the server can be exercised end to end with no hardware: the reference device
(an Osmo Pocket 4P in webcam mode) was measured, returned to the shop, and every number this
module uses comes from that session rather than from invention.

What is modelled, and why each one matters:

  * **latency** -- ~0.4s between a write and the first visible motion.
  * **settle time per axis** -- a pan completes in about 1s, a 4x zoom in about 2.5s. A server
    that waits the same amount for both is either slow or wrong.
  * **silently dropped writes** -- observed: a command to pan to 180 left the picture unchanged
    while the device reported 180. The simulator can drop a write and *then report the requested
    value*, which is the trap the whole verification design exists to catch.
  * **frames that actually move** -- the view is a crop of a wide panorama, so a commanded pan
    changes the pixels. A simulator that returned a static picture would let verification pass
    vacuously, which is worse than no simulator.

The panorama is synthesised deterministically, so tests are reproducible. Point
`UVC_PTZ_SIM_SOURCE` at a real image to use that instead (a frame from the actual camera makes
the simulation more honest, and it is what the tests for the metrics were validated against).
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .camera import REFERENCE_SPECS, Axis, AxisSpec
from .motion import ease_progress

# Measured on the reference device.
LATENCY_SECONDS = 0.4
SETTLE_SECONDS: dict[Axis, float] = {
    Axis.PAN: 1.0,
    Axis.TILT: 1.0,
    Axis.ROLL: 0.8,
    Axis.ZOOM: 2.5,
}
DROP_PROBABILITY = 0.12
# Zoom counts in hundredths of a multiplier: 100 is 1x, 300 is 3x.
ZOOM_UNITS_PER_X = 100
# A supplied image smaller than this is treated as unusable and the synthetic panorama is
# used instead: a 40-pixel-tall 'panorama' would make every pose crop identical.
MIN_PANORAMA_HEIGHT = 100

# How much of the panorama the camera sees at 1x. The panorama is the world it can turn through,
# so this is what makes pan and tilt change the picture at all.
PANORAMA_FIELD_DIVISOR = 4.0

# Frame geometry. The panorama is what the camera could see if it turned; a pose selects a crop.
PANORAMA_WIDTH = 4096
PANORAMA_HEIGHT = 1024
FRAME_HEIGHT = 180
FRAME_WIDTH = 320
NOISE_GREY_LEVELS = 1.2


def synthesise_panorama(width: int = PANORAMA_WIDTH, height: int = PANORAMA_HEIGHT) -> np.ndarray:
    """Build a wide, deterministic, detailed image for poses to crop from.

    Detail matters: a flat image would let a slow pan produce almost no pixel change, which is
    the measurement mistake that wasted an evening on the real device (a pan across a blank wall
    is nearly invisible to a frame-difference metric).
    """
    rng = np.random.default_rng(20260926)
    base = rng.normal(110.0, 22.0, size=(height, width))
    # large-scale structure, so a crop is recognisably a place rather than a uniform field
    yy, xx = np.mgrid[0:height, 0:width]
    base += 25.0 * np.sin(xx / 120.0) + 18.0 * np.cos(yy / 70.0)
    # strong vertical edges every so often -- these are what an edge tracker or a shift estimate
    # latches onto, and what makes "did the picture move" unambiguous
    for x in range(200, width, 400):
        base[:, x : x + 3] -= 70.0
    for x in range(100, width, 400):
        base[:, x : x + 2] += 45.0
    return np.clip(base, 0, 255)


class SimulatorBackend:
    """A PTZ camera that behaves like the measured reference, with no hardware."""

    name = "simulator"

    def __init__(  # noqa: PLR0917 - a configuration record, always called with keywords
        self,
        seed: int = 7,
        clock: Callable[[], float] = time.monotonic,
        drop_probability: float = DROP_PROBABILITY,
        apply_pattern: list[bool] | None = None,
        source: str | Path | None = None,
        specs: dict[Axis, AxisSpec] | None = None,
        speed: float = 1.0,
    ) -> None:
        """Configure the simulated camera: timing, dropped writes, and its panorama."""
        self._clock = clock
        self._rng = np.random.default_rng(seed)
        self._drop_probability = drop_probability
        self._apply_pattern = list(apply_pattern) if apply_pattern else None
        self._source = Path(source) if source else None
        self._specs = dict(specs or REFERENCE_SPECS)
        # Compresses latency and settle times without changing what the model does. A test suite
        # that has to sit through the measured 1.4s-per-move would either be slow or start
        # skipping assertions, so the speed is a parameter rather than a fixed cost.
        self._speed = max(0.001, float(speed))

        self.fallback_reason: str | None = None
        self.actions = 0
        self.dropped = 0
        self.writes: list[tuple[Axis, int, bool]] = []

        self._pose: dict[Axis, float] = {axis: spec.default for axis, spec in self._specs.items()}
        self._target: dict[Axis, float] = dict(self._pose)
        self._move_started: dict[Axis, float] = dict.fromkeys(self._specs, 0.0)
        self._move_from: dict[Axis, float] = dict(self._pose)
        self._last_request: dict[Axis, tuple[int, bool]] = {}
        self._panorama: np.ndarray | None = None
        self._opened = False

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        """Prepare the panorama. Never fails: this backend is always available."""
        self._panorama = self._load_panorama()
        self._opened = True

    def close(self) -> None:
        """Release state. Safe to call twice."""
        self._opened = False

    def _load_panorama(self) -> np.ndarray:
        """Use a real image when one is supplied, otherwise synthesise a deterministic one."""
        candidate = self._source or (os.environ.get("UVC_PTZ_SIM_SOURCE") or None)
        if candidate and Path(candidate).exists():
            try:
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-i",
                        str(candidate),
                        "-vf",
                        f"scale={PANORAMA_WIDTH}:-2",
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "gray",
                        "-",
                    ],
                    capture_output=True,
                    timeout=60,
                    check=True,
                )
                width = PANORAMA_WIDTH
                height = len(result.stdout) // width
                if width and height > MIN_PANORAMA_HEIGHT:
                    pixels = np.frombuffer(result.stdout, dtype=np.uint8)
                    return pixels.reshape(height, width).astype(np.float64)
            except Exception:  # noqa: BLE001 - fall back to the synthetic panorama
                pass
        return synthesise_panorama()

    # -- contract ----------------------------------------------------------
    def specs(self) -> dict[Axis, AxisSpec]:
        """Report the reference device's advertised ranges."""
        return dict(self._specs)

    def settle_seconds(self, axis: Axis) -> float:
        """Measured settle time for this axis, plus the latency before motion starts."""
        return (LATENCY_SECONDS + SETTLE_SECONDS.get(axis, 1.0)) / self._speed

    def read_back_lag(self) -> int:
        """Report how far the device's value was measured lagging the command."""
        return 1

    def write(self, axis: Axis, value: int) -> None:
        """Command an absolute position, possibly dropping it exactly as the device did."""
        spec = self._specs[axis]
        target = spec.clamp(value)
        applied = self._should_apply()
        self.actions += 1
        self.writes.append((axis, target, applied))

        # The device 'reported' every request, applied or not. That is the lie: a caller reading
        # this back cannot distinguish "moved" from "was told to move".
        self._last_request[axis] = (target, applied)

        if not applied:
            self.dropped += 1
            return

        self._move_from[axis] = self._current(axis)
        self._target[axis] = target
        # The latency is compressed by the same factor as the settle time, or a fast test
        # configuration would sleep for less than the modelled delay and see no movement at all.
        self._move_started[axis] = self._clock() + (LATENCY_SECONDS / self._speed)

    def read(self, axis: Axis) -> int | None:
        """Report the device's own value -- a hint, echoing a request it may never have applied."""
        if axis in self._last_request:
            reported, applied = self._last_request[axis]
            if not applied:
                # the device happily reports a value it never moved to (observed: pan 180)
                return reported
            if self._clock() < self._move_started.get(axis, 0.0) + SETTLE_SECONDS.get(axis, 1.0):
                # still travelling: report the last commanded target, as the device did
                return reported
        return int(round(self._current(axis)))

    def frame(self) -> np.ndarray:
        """Render what the camera sees from the current pose."""
        if self._panorama is None:
            self.open()
        assert self._panorama is not None
        height, width = self._panorama.shape

        zoom = self._pose[Axis.ZOOM] / ZOOM_UNITS_PER_X if Axis.ZOOM in self._specs else 1.0
        # At 1x the camera sees a fraction of the panorama; zooming shrinks that window further.
        # Getting this wrong is not a cosmetic bug: when the crop covered the whole panorama, the
        # crop offset was always zero and every pose rendered the identical frame, so no move
        # could ever be verified.
        crop_width = int(width / (PANORAMA_FIELD_DIVISOR * max(1.0, zoom)))
        crop_width = max(64, min(crop_width, width))
        crop_height = min(height, max(64, int(crop_width * FRAME_HEIGHT / FRAME_WIDTH)))

        # Map pose onto panorama position. Both axes use their full advertised travel across the
        # panorama, so a commanded move always moves the picture.
        pan_spec = self._specs.get(Axis.PAN)
        tilt_spec = self._specs.get(Axis.TILT)
        pan_fraction = 0.5
        if pan_spec and pan_spec.maximum > pan_spec.minimum:
            pan_fraction = (self._current(Axis.PAN) - pan_spec.minimum) / (
                pan_spec.maximum - pan_spec.minimum
            )
        tilt_fraction = 0.5
        if tilt_spec and tilt_spec.maximum > tilt_spec.minimum:
            tilt_fraction = (self._current(Axis.TILT) - tilt_spec.minimum) / (
                tilt_spec.maximum - tilt_spec.minimum
            )

        left = int(round(pan_fraction * (width - crop_width)))
        top = int(round((1.0 - tilt_fraction) * (height - crop_height)))
        crop = self._panorama[top : top + crop_height, left : left + crop_width]

        # Resample to the output size (nearest is fine: this is a test fixture, not a picture)
        rows = np.linspace(0, crop.shape[0] - 1, FRAME_HEIGHT).astype(int)
        cols = np.linspace(0, crop.shape[1] - 1, FRAME_WIDTH).astype(int)
        image = crop[np.ix_(rows, cols)]
        image = image + self._rng.normal(0.0, NOISE_GREY_LEVELS, size=image.shape)
        return np.clip(image, 0, 255).astype(np.uint8)

    def describe(self) -> dict:
        """Backend description for the status tool, including whether it stands in for hardware."""
        return {
            "backend": self.name,
            "simulated": True,
            "stand_in_for_hardware": self.fallback_reason is not None,
            "reason": self.fallback_reason or "selected explicitly",
            "writes": self.actions,
            "writes_dropped": self.dropped,
            "modelled_from": (
                "a DJI Osmo Pocket 4P in webcam mode: ~0.4s latency, pan ~1s, 4x zoom ~2.5s, "
                "dropped writes, a read-back that echoes unapplied requests"
            ),
        }

    # -- internals ---------------------------------------------------------
    def _should_apply(self) -> bool:
        """Decide whether this write lands.

        `apply_pattern` takes precedence when set, and its values read literally: True means the
        write reaches the hardware, False means it is silently dropped. Naming it for what
        happens (rather than for the failure) keeps a test's intent and its data in agreement --
        the first version of this parameter was called `drop_pattern` with inverted meaning, and
        two tests quietly asserted the opposite of what they read as.
        """
        if self._apply_pattern is not None and self._apply_pattern:
            return self._apply_pattern.pop(0)
        return bool(self._rng.random() >= self._drop_probability)

    def _current(self, axis: Axis) -> float:
        """Where the axis is now, easing from the last position to the target."""
        started = self._move_started.get(axis, 0.0)
        target = self._target.get(axis, self._pose.get(axis, 0.0))
        origin = self._move_from.get(axis, target)
        duration = SETTLE_SECONDS.get(axis, 1.0) / self._speed
        elapsed = self._clock() - started
        if elapsed <= 0:
            return origin
        if elapsed >= duration:
            self._pose[axis] = target
            return target
        return origin + (target - origin) * ease_progress("in_out", elapsed / duration)

    def set_pose_for_test(self, **values: int) -> None:
        """Teleport the simulated camera. Test-only: keeps fixtures from depending on timing."""
        for name, value in values.items():
            axis = Axis(name)
            if axis in self._specs:
                self._pose[axis] = self._specs[axis].clamp(value)
                self._target[axis] = self._pose[axis]
                self._move_from[axis] = self._pose[axis]
