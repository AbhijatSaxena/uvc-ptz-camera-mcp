"""Layer 1b: the verdict function, tested against the calibration that produced its threshold.

Two things are asserted here. First that the metric behaves: identical frames score zero, a
shifted frame scores high, and a pure brightness change scores near zero -- that last one is the
property that makes the metric usable at all, because auto-exposure hunting otherwise looks
exactly like movement to a naive difference.

Second, that the shipped threshold still separates the labelled pairs measured on the reference
camera. If someone changes the metric or the threshold to something that no longer reproduces
that calibration, this fails rather than a tool quietly reporting the wrong verdict.
"""

from __future__ import annotations

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
