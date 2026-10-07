"""Decide whether two frames show the same view, using a threshold calibrated on hardware.

The metric is a z-normalised mean absolute difference: each frame is centred and scaled to
unit variance before comparison, so a global brightness or contrast shift -- which is what
auto-exposure hunting looks like -- cannot score as movement. Pixel differences from exposure
are the main false positive a naive metric produces.

The default threshold is not a guess. It comes from labelled frames captured from the
reference camera (a DJI Osmo Pocket 4P), where the ground truth was known independently:

    highest value across "same view" pairs      0.104   (quiet baseline 96s apart; two
                                                         commanded-but-not-applied moves)
    lowest value across "view changed" pairs    0.624   (Pan -38, Tilt -30, 12x zoom, and one
                                                         unexplained view change)
    chosen threshold                            0.364   (midpoint: a 6x separation)

`DEFAULT_THRESHOLD` is that midpoint. `reference_cases()` returns the recorded pairs, and the
test suite asserts the threshold still separates them -- so if the metric is ever changed to
something that no longer reproduces the calibration, a test fails rather than a tool quietly
reporting the wrong verdict.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Chosen as the midpoint of the recorded same/changed clusters; see the module docstring.
DEFAULT_THRESHOLD = 0.364

# What the calibration was measured on, for honest reporting.
REFERENCE_HIGHEST_SAME = 0.104
REFERENCE_LOWEST_CHANGED = 0.624

# Recorded from the reference device. The frames behind these numbers are in tests/fixtures/;
# this is the same record in code, so that anyone reading the metric sees where the threshold came
# from without opening a binary file. Both copies are asserted to agree in tests/test_verify.py.
_REFERENCE_CASES: tuple[tuple[str, str, str, float], ...] = (
    ("q_00.png", "q_01.png", "same", 0.028),
    ("q_00.png", "q_06.png", "same", 0.031),
    ("q_00.png", "q_11.png", "same", 0.032),
    ("s_0.png", "s_60.png", "same", 0.034),
    ("v2_0_0.png", "v2_1_60.png", "same", 0.104),
    ("v2_0_0.png", "v2_2_0.png", "same", 0.087),
    ("w_baseline.png", "w_2_pan-38.png", "changed", 0.969),
    ("w_baseline.png", "w_5_tilt-30.png", "changed", 0.624),
    ("w_baseline.png", "w_6_zoom1200.png", "changed", 1.616),
    ("s_0.png", "s_0b.png", "changed", 1.205),
)


@dataclass(frozen=True)
class Verdict:
    """The result of comparing two frames."""

    changed: bool
    value: float
    threshold: float
    same_as: str | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        """Serialise for a tool result."""
        return {
            "view_changed": self.changed,
            "difference": round(self.value, 4),
            "threshold": self.threshold,
            "detail": self.detail,
        }


def reference_cases() -> tuple[tuple[str, str, str, float], ...]:
    """Return the labelled (first, second, label, value) pairs behind the threshold."""
    return _REFERENCE_CASES


def z_mad(first: np.ndarray, second: np.ndarray) -> float:
    """Z-normalised mean absolute difference between two greyscale frames.

    Returns a value on roughly the 0-2 scale the calibration uses: ~0.03 for the same static
    view, 0.6-1.6 for a view that has really moved.
    """
    a = np.asarray(first, dtype=np.float64).ravel()
    b = np.asarray(second, dtype=np.float64).ravel()
    if a.size == 0 or b.size == 0:
        raise ValueError("cannot compare an empty frame")
    size = min(a.size, b.size)
    a, b = a[:size], b[:size]
    a = (a - a.mean()) / (a.std() + 1e-6)
    b = (b - b.mean()) / (b.std() + 1e-6)
    return float(np.mean(np.abs(a - b)))


def compare(
    before: np.ndarray,
    after: np.ndarray,
    threshold: float = DEFAULT_THRESHOLD,
    detail: str = "",
) -> Verdict:
    """Compare two frames and say whether the view moved.

    `changed is False` is a normal outcome, not a failure: it means the camera is already where
    it was asked to be, or that the write never reached it. The caller decides which by
    comparing the requested value with the one the device last reported -- never by assuming
    that a successful write moved anything.
    """
    value = z_mad(before, after)
    return Verdict(changed=value > threshold, value=value, threshold=threshold, detail=detail)
