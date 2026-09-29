"""The backend contract: what a camera driver must provide, and the errors a caller can get.

Two implementations ship: `dshow` (Windows DirectShow, the real device) and `simulator`
(calibrated from the reference hardware, so the server is exercisable with no camera attached).

The contract deliberately exposes the device's reported value as `read` -- a *hint* -- while
`frame` is what the server actually trusts. On the reference device the reported value was
measured lying in both directions, so a backend that could only report angles would make an
honest server impossible.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

from .camera import Axis, AxisSpec, Pose


class BackendError(RuntimeError):
    """Base class for the failures a backend can raise."""


class BackendUnavailableError(BackendError):
    """No usable device, or the dependency needed to talk to one is missing.

    Raised at open time (or reported by `status`) rather than at import, so that a server with
    no camera still starts, answers `tools/list`, and explains itself instead of dying.
    """


class MoveUnconfirmedError(BackendError):
    """A command was sent, the device accepted it, and the picture says nothing moved.

    This is the failure the whole design exists to surface. The device reported success; the
    evidence disagrees; the caller must not be told the move happened.
    """


@runtime_checkable
class CameraBackend(Protocol):
    """What every camera driver provides. Implementations are used from a worker thread."""

    name: str

    def open(self) -> None:
        """Acquire the device. Raises BackendUnavailableError if there is nothing to talk to."""

    def close(self) -> None:
        """Release the device. Must be safe to call more than once."""

    def specs(self) -> dict[Axis, AxisSpec]:
        """Report the axes this camera exposes and the ranges it advertises."""

    def read(self, axis: Axis) -> int | None:
        """Report where the device believes `axis` is. A hint, never proof. None if unknown."""

    def write(self, axis: Axis, value: int) -> None:
        """Ask the device to move `axis` to `value`. Must be clamped by the caller."""

    def settle_seconds(self, axis: Axis) -> float:
        """How long a command on `axis` has been measured to take before the picture settles."""

    def read_back_lag(self) -> int:
        """How many interactions the device's `read` is known to lag behind by."""

    def frame(self) -> np.ndarray:
        """One greyscale frame of what the camera currently sees.

        This is the only trustworthy observation the server has, so a backend that cannot
        produce frames cannot support verified moves -- it should raise rather than return a
        placeholder, which would silently verify everything.
        """

    def describe(self) -> dict:
        """Describe the backend honestly, for the status tool."""


def snapshot_pose(backend: CameraBackend) -> Pose:
    """Read every axis the backend exposes into a Pose."""
    specs = backend.specs()
    values: dict[Axis, int] = {}
    for axis, spec in specs.items():
        reported = backend.read(axis)
        values[axis] = spec.default if reported is None else int(reported)
    return Pose(values)


def open_backend(kind: str = "auto", device: str | None = None) -> CameraBackend:
    """Open a backend by name: "simulator", "dshow", or "auto" to prefer a real device.

    "auto" tries DirectShow and falls back to the simulator, recording which one it used so
    `status` can say so. Silent fallback would be worse than useless: a caller must never
    believe it is driving hardware when it is driving a model.
    """
    kind = (kind or "auto").strip().lower()
    if kind == "simulator":
        # noqa reason: imported here so the package works with no optional extras installed
        from .simulator import SimulatorBackend  # noqa: PLC0415

        return SimulatorBackend()
    if kind == "dshow":
        from .dshow import DshowBackend  # noqa: PLC0415 - keeps the Windows-only extra optional

        return DshowBackend(device=device)
    if kind != "auto":
        raise ValueError(f'unknown backend {kind!r}; expected "auto", "dshow" or "simulator"')

    try:
        from .dshow import DshowBackend  # noqa: PLC0415 - optional extra

        backend = DshowBackend(device=device)
        backend.open()
        return backend
    except Exception as error:  # noqa: BLE001 - any failure means "no real camera here"
        from .simulator import SimulatorBackend  # noqa: PLC0415 - the fallback path

        simulated = SimulatorBackend()
        simulated.fallback_reason = f"{type(error).__name__}: {error}"
        return simulated
