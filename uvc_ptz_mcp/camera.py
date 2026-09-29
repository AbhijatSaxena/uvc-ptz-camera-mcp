"""The camera domain model: axes, their advertised ranges, and a pose on them.

Everything here is hardware-independent. The numbers in `REFERENCE_SPECS` are the ranges a
DJI Osmo Pocket 4P advertises over UVC in webcam mode, measured directly (see the project's
FINDINGS). They are a *reference*, not an assumption: a backend reports the ranges it reads
from the device, and every commanded value is clamped to those before it is sent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

DEGREES = "degrees"
RATIO_X100 = "ratio_x100"


class Axis(str, Enum):
    """The axes a PTZ camera may expose.

    Subclasses `str` rather than `enum.StrEnum`, which only exists from Python 3.11 and would
    break the 3.10 floor this package claims.
    """

    PAN = "pan"
    TILT = "tilt"
    ROLL = "roll"
    ZOOM = "zoom"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        """Return the axis name, so f-strings read as "pan" not "Axis.PAN"."""
        return self.value


@dataclass(frozen=True)
class AxisSpec:
    """One axis: the range the device advertised, plus how to read its numbers.

    `unit` matters to the caller. Gimbal axes are degrees. Zoom on the reference device is
    `ratio_x100`: 100 is 1x and 1200 is 12x, so a caller asking for "3x" must send 300 and a
    caller reading "1200" must understand it as 12x, not 1200x.
    """

    axis: Axis
    minimum: int
    maximum: int
    step: int
    default: int
    unit: str = DEGREES

    def clamp(self, value: float) -> int:
        """Return `value` as an integer inside the advertised range.

        Input is validated here rather than trusted: a caller passing 900 to a pan axis whose
        ceiling is 215 gets 215, not a wrapped or truncated byte.
        """
        if math.isnan(value):
            raise ValueError(f"{self.axis}: value must be a number, got {value!r}")
        return int(round(max(self.minimum, min(self.maximum, value))))

    def contains(self, value: float) -> bool:
        """Say whether `value` is inside the advertised range, with no clamping applied."""
        return self.minimum <= value <= self.maximum

    def to_dict(self) -> dict:
        """Serialise for a tool result."""
        return {
            "axis": self.axis.value,
            "min": self.minimum,
            "max": self.maximum,
            "step": self.step,
            "default": self.default,
            "unit": self.unit,
        }


# The reference device's advertised ranges, as measured.
REFERENCE_SPECS: dict[Axis, AxisSpec] = {
    Axis.PAN: AxisSpec(Axis.PAN, -38, 215, 1, 0),
    Axis.TILT: AxisSpec(Axis.TILT, -33, 105, 1, 0),
    Axis.ROLL: AxisSpec(Axis.ROLL, -35, 35, 1, 0),
    Axis.ZOOM: AxisSpec(Axis.ZOOM, 100, 1200, 1, 100, unit=RATIO_X100),
}


@dataclass
class Pose:
    """A position on every axis the device exposes."""

    values: dict[Axis, int] = field(default_factory=dict)

    @classmethod
    def neutral(cls, specs: dict[Axis, AxisSpec]) -> Pose:
        """Return the device's own default on each axis: centre, unzoomed."""
        return cls({axis: spec.default for axis, spec in specs.items()})

    def get(self, axis: Axis) -> int | None:
        """Return the value on `axis`, or None when the axis is unknown to this device."""
        return self.values.get(axis)

    def with_axis(self, axis: Axis, value: int) -> Pose:
        """Return a copy of this pose with one axis changed."""
        updated = dict(self.values)
        updated[axis] = value
        return Pose(updated)

    def to_dict(self) -> dict[str, int]:
        """Serialise with axis names as keys, for tool output."""
        return {axis.value: value for axis, value in self.values.items()}


def parse_axis(name: str) -> Axis:
    """Turn a caller-supplied axis name into an Axis, or explain what is allowed."""
    try:
        return Axis(str(name).strip().lower())
    except ValueError:
        allowed = ", ".join(axis.value for axis in Axis)
        raise ValueError(f"unknown axis {name!r}; expected one of: {allowed}") from None
