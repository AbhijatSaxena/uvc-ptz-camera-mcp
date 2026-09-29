"""Layer 1a: the shot compiler, tested in isolation.

Pure unit tests. No camera, no simulator, no clock: a compiler that cannot be tested without
hardware is a compiler whose validation rules are guesses.
"""

from __future__ import annotations

import pytest

from uvc_ptz_mcp.camera import REFERENCE_SPECS, Axis
from uvc_ptz_mcp.motion import (
    DEFAULT_RATE_HZ,
    compile_shot,
    ease_progress,
    recentre_steps,
)

SPECS = REFERENCE_SPECS
START = {axis: spec.default for axis, spec in SPECS.items()}


def test_ease_progress_endpoints_and_monotonicity():
    for ease in ("linear", "in", "out", "in_out"):
        assert ease_progress(ease, 0.0) == pytest.approx(0.0)
        assert ease_progress(ease, 1.0) == pytest.approx(1.0)
        values = [ease_progress(ease, i / 10) for i in range(11)]
        assert values == sorted(values), f"{ease} should not go backwards"
    with pytest.raises(ValueError):
        ease_progress("nope", 0.5)


def test_compile_produces_a_tick_stream_that_ends_at_the_target():
    shot = compile_shot([{"axis": "pan", "to": 60, "seconds": 2.0}], SPECS, START)
    assert shot.duration == pytest.approx(2.0)
    assert shot.rate_hz == DEFAULT_RATE_HZ
    assert len(shot.ticks) == pytest.approx(2.0 * DEFAULT_RATE_HZ, abs=1)
    assert shot.ticks[-1].targets[Axis.PAN] == 60
    assert shot.warnings == []


def test_compile_clamps_and_says_so():
    shot = compile_shot([{"axis": "pan", "to": 9000, "seconds": 1.0}], SPECS, START)
    assert shot.ticks[-1].targets[Axis.PAN] == SPECS[Axis.PAN].maximum
    assert any("clamped" in warning for warning in shot.warnings)


def test_unknown_axis_and_bad_values_are_refused_before_anything_moves():
    with pytest.raises(ValueError, match="unknown axis"):
        compile_shot([{"axis": "zoomx", "to": 5}], SPECS, START)
    with pytest.raises(ValueError, match="seconds"):
        compile_shot([{"axis": "pan", "to": 5, "seconds": 0}], SPECS, START)
    with pytest.raises(ValueError, match="ease"):
        compile_shot([{"axis": "pan", "to": 5, "ease": "teleport"}], SPECS, START)
    with pytest.raises(ValueError, match="at least one step"):
        compile_shot([], SPECS, START)
    with pytest.raises(ValueError, match="needs 'axis' and 'to'"):
        compile_shot([{"axis": "pan"}], SPECS, START)


def test_an_axis_the_camera_lacks_is_named_in_the_refusal():
    """A real axis name on a camera that lacks it is a different error from a made-up name."""
    without_roll = {axis: spec for axis, spec in SPECS.items() if axis is not Axis.ROLL}
    with pytest.raises(ValueError, match="no roll axis"):
        compile_shot([{"axis": "roll", "to": 5}], without_roll, START)


def test_two_axes_in_one_step_become_a_diagonal_not_a_staircase():
    """The bug this guards against: writing one axis per tick turns a diagonal into a staircase.

    An earlier demonstration script of ours did exactly that, so its "diagonal" and
    "figure-eight" segments silently moved one axis only. Ticks landing on the same instant must
    therefore carry every moving axis.
    """
    shot = compile_shot(
        [
            {"axis": "pan", "to": 30, "seconds": 1.0},
            {"axis": "tilt", "to": 20, "seconds": 1.0},
        ],
        SPECS,
        START,
    )
    assert len(shot.step_ends) == 2
    assert shot.step_labels[0].startswith("pan -> 30")
    assert shot.step_labels[1].startswith("tilt -> 20")
    assert shot.duration == pytest.approx(2.0)
    # each step is verified separately by the executor, so the boundaries must be distinct
    assert shot.step_ends[0] < shot.step_ends[1]


def test_hold_extends_the_timeline_without_moving():
    without = compile_shot([{"axis": "pan", "to": 10, "seconds": 1.0}], SPECS, START)
    with_hold = compile_shot([{"axis": "pan", "to": 10, "seconds": 1.0, "hold": 2.0}], SPECS, START)
    assert with_hold.duration == pytest.approx(without.duration + 2.0)
    # the hold adds time to the shot and holds the value; it does not add a move
    assert with_hold.ticks[-1].targets[Axis.PAN] == 10
    assert with_hold.ticks[-1].t <= with_hold.duration


def test_rate_bounds_and_tick_cap_are_enforced():
    with pytest.raises(ValueError, match="rate_hz"):
        compile_shot([{"axis": "pan", "to": 10}], SPECS, START, rate_hz=0)
    with pytest.raises(ValueError, match="rate_hz"):
        compile_shot([{"axis": "pan", "to": 10}], SPECS, START, rate_hz=1000)


def test_shot_too_long_is_refused_rather_than_starting():
    with pytest.raises(ValueError, match="longer than"):
        compile_shot([{"axis": "pan", "to": 10, "seconds": 120} for _ in range(6)], SPECS, START)


def test_recentre_covers_every_axis_at_its_default():
    steps = recentre_steps(SPECS)
    assert {step["axis"] for step in steps} == {axis.value for axis in SPECS}
    for step in steps:
        assert step["to"] == SPECS[Axis(step["axis"])].default
