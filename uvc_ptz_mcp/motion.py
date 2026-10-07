"""Compile a shot description into a stream of absolute targets.

The device eats *absolute* targets, not velocity commands: every write says "be at this angle".
Smooth motion therefore comes from streaming small successive targets at a steady rate, which is
what the reference device was measured doing well (364 streamed writes, zero failures, ~0.4s
from command to first motion, a pan completing in about a second).

That is what this module does: a caller describes a shot ("pan to 60 over two seconds, ease
in-out, then hold"), and the compiler turns it into timed ticks a backend can execute. It also
refuses, up-front, anything the device cannot do, so an invalid shot fails before the camera
moves rather than halfway through.

One correction worth stating: an earlier demonstration script of ours wrote only one axis per
tick, so its "diagonal" and "figure-eight" segments silently moved the pan axis only. This
compiler writes *every* moving axis on each tick, which is what those segments were meant to do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import cos, pi

from .camera import Axis, AxisSpec, parse_axis

DEFAULT_RATE_HZ = 15.0
MAX_RATE_HZ = 60.0
MAX_STEP_SECONDS = 300.0
MAX_SHOT_SECONDS = 600.0
MAX_TICKS = 20_000

EASES = ("linear", "in_out", "out", "in")


def ease_progress(ease: str, u: float) -> float:
    """Map linear progress `u` in [0, 1] onto the eased progress for `ease`."""
    if not 0.0 <= u <= 1.0:
        raise ValueError(f"progress must be within [0, 1], got {u!r}")
    if ease == "linear":
        return u
    if ease == "in":
        return u * u
    if ease == "out":
        return 1.0 - (1.0 - u) ** 2
    if ease == "in_out":
        # smootherstep-style: zero velocity at both ends, which is what makes a move read as
        # deliberate rather than as a snap that happens to end in the right place
        return 0.5 - 0.5 * cos(pi * u)
    raise ValueError(f"unknown ease {ease!r}; expected one of: {', '.join(EASES)}")


@dataclass(frozen=True)
class Step:
    """One movement in a shot: go to `to` on `axis` over `seconds`, then optionally hold."""

    axis: str
    to: int
    seconds: float = 1.0
    ease: str = "in_out"
    hold: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict) -> Step:
        """Build a step from a caller-supplied mapping, validating the types it needs."""
        if not isinstance(raw, dict):
            raise ValueError(f"each step must be an object, got {type(raw).__name__}")
        if "axis" not in raw or "to" not in raw:
            raise ValueError("each step needs 'axis' and 'to'")
        return cls(
            axis=str(raw["axis"]),
            to=int(raw["to"]),
            seconds=float(raw.get("seconds", 1.0)),
            ease=str(raw.get("ease", "in_out")),
            hold=float(raw.get("hold", 0.0)),
        )


@dataclass(frozen=True)
class Tick:
    """One moment in the compiled shot: the value every moving axis should be at."""

    t: float
    targets: dict[Axis, int]


@dataclass(frozen=True)
class CompiledStep:
    """One step as the compiler understood it: validated, clamped, and costed.

    Kept so a caller can ask what a shot *would* do before anything moves -- which is the point of
    separating planning from execution. Without it, the only way to find out what a step list does
    is to run it and watch.
    """

    index: int
    axis: Axis
    origin: int
    target: int
    requested: int
    seconds: float
    ease: str
    hold: float
    ticks: int

    def to_dict(self) -> dict:
        """Serialise for a tool result."""
        return {
            "step": self.index,
            "axis": self.axis.value,
            "from": self.origin,
            "to": self.target,
            "travel": self.target - self.origin,
            "seconds": round(self.seconds, 3),
            "ease": self.ease,
            "hold": round(self.hold, 3),
            "ticks": self.ticks,
            "clamped": self.target != self.requested,
            "requested": self.requested,
        }


@dataclass
class Shot:
    """A compiled shot, ready to execute."""

    ticks: list[Tick] = field(default_factory=list)
    duration: float = 0.0
    rate_hz: float = DEFAULT_RATE_HZ
    warnings: list[str] = field(default_factory=list)
    steps: int = 0
    # Where each step ends on the timeline, and what it was, so an executor can verify after
    # every waypoint rather than only at the end of the shot.
    step_ends: list[float] = field(default_factory=list)
    step_labels: list[str] = field(default_factory=list)
    # The steps themselves, with the origin each one started from and the value it was clamped to.
    compiled: list[CompiledStep] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialise the plan (not every tick) for a tool result."""
        return {
            "duration_seconds": round(self.duration, 3),
            "rate_hz": self.rate_hz,
            "ticks": len(self.ticks),
            "steps": self.steps,
            "schedule": [step.to_dict() for step in self.compiled],
            "warnings": self.warnings,
        }

    def envelope(self) -> dict[str, dict]:
        """Per-axis summary: where it starts, the extremes it reaches, and where it ends."""
        summary: dict[str, dict] = {}
        for step in self.compiled:
            entry = summary.setdefault(
                step.axis.value,
                {
                    "starts_at": step.origin,
                    "ends_at": step.target,
                    "lowest": step.origin,
                    "highest": step.origin,
                    "steps": 0,
                    "seconds": 0.0,
                },
            )
            entry["ends_at"] = step.target
            entry["lowest"] = min(entry["lowest"], step.origin, step.target)
            entry["highest"] = max(entry["highest"], step.origin, step.target)
            entry["steps"] += 1
            entry["seconds"] = round(entry["seconds"] + step.seconds + step.hold, 3)
        for entry in summary.values():
            entry["travel"] = entry["ends_at"] - entry["starts_at"]
        return summary

    def ticks_until(self, limit: float) -> list[Tick]:
        """Ticks up to and including time `limit`."""
        return [tick for tick in self.ticks if tick.t <= limit + 1e-9]


def _validated_step(
    index: int,
    raw: dict,
    specs: dict[Axis, AxisSpec],
) -> tuple[Step, Axis, AxisSpec, int, list[str]]:
    """Validate one step against the device's ranges, returning what the compiler needs.

    Every refusal happens here, before a single tick is emitted, so an impossible shot fails
    without the camera moving.
    """
    step = Step.from_dict(raw)
    axis = parse_axis(step.axis)
    spec = specs.get(axis)
    if spec is None:
        available = ", ".join(a.value for a in specs)
        raise ValueError(
            f"step {index}: this camera has no {axis.value} axis (available: {available})"
        )
    if not 0.0 < step.seconds <= MAX_STEP_SECONDS:
        raise ValueError(
            f"step {index}: seconds must be within (0, {MAX_STEP_SECONDS}], got {step.seconds}"
        )
    if step.hold < 0 or step.hold > MAX_STEP_SECONDS:
        raise ValueError(f"step {index}: hold must be within [0, {MAX_STEP_SECONDS}]")
    if step.ease not in EASES:
        raise ValueError(f"step {index}: unknown ease {step.ease!r}; expected {EASES}")

    target = spec.clamp(step.to)
    warnings: list[str] = []
    if target != int(round(step.to)):
        warnings.append(
            f"step {index}: {axis.value} {step.to} clamped to {target} "
            f"(device range {spec.minimum}..{spec.maximum})"
        )
    return step, axis, spec, target, warnings


def _step_ticks(  # noqa: PLR0917 - a pure helper; each argument is a distinct input
    spec: AxisSpec,
    axis: Axis,
    origin: int,
    target: int,
    step: Step,
    rate_hz: float,
    timeline: float,
) -> list[Tick]:
    """Turn one step into its ticks, easing from origin to target."""
    ticks: list[Tick] = []
    tick_count = max(1, int(round(step.seconds * rate_hz)))
    for tick_index in range(1, tick_count + 1):
        u = tick_index / tick_count
        value = spec.clamp(round(origin + (target - origin) * ease_progress(step.ease, u)))
        ticks.append(Tick(t=timeline + step.seconds * u, targets={axis: value}))
    return ticks


def compile_shot(
    raw_steps: list[dict],
    specs: dict[Axis, AxisSpec],
    start: dict[Axis, int],
    rate_hz: float = DEFAULT_RATE_HZ,
) -> Shot:
    """Compile steps into ticks, validating everything against what the device advertises.

    `start` is where the camera is believed to be: the compiler needs it to interpolate from,
    and it comes from the device's own report. If that report is wrong the shot starts from the
    wrong place, which is why the executor verifies the first waypoint against the picture.
    """
    if not raw_steps:
        raise ValueError("a shot needs at least one step")
    if not 1.0 <= rate_hz <= MAX_RATE_HZ:
        raise ValueError(f"rate_hz must be within [1, {MAX_RATE_HZ}], got {rate_hz}")

    shot = Shot(rate_hz=rate_hz, steps=len(raw_steps))
    timeline = 0.0
    current = dict(start)

    for index, raw in enumerate(raw_steps):
        step, axis, spec, target, warnings = _validated_step(index, raw, specs)
        shot.warnings.extend(warnings)

        origin = current.get(axis, spec.default)
        step_ticks = _step_ticks(spec, axis, origin, target, step, rate_hz, timeline)
        shot.ticks.extend(step_ticks)
        if len(shot.ticks) > MAX_TICKS:
            raise ValueError(
                f"shot would need more than {MAX_TICKS} ticks; shorten it or lower rate_hz"
            )
        shot.compiled.append(
            CompiledStep(
                index=index,
                axis=axis,
                origin=origin,
                target=target,
                requested=int(round(step.to)),
                seconds=step.seconds,
                ease=step.ease,
                hold=step.hold,
                ticks=len(step_ticks),
            )
        )

        current[axis] = target
        timeline += step.seconds
        if step.hold:
            shot.ticks.append(Tick(t=timeline, targets={axis: target}))
            timeline += step.hold
        shot.step_ends.append(timeline)
        shot.step_labels.append(f"{axis.value} -> {target} over {step.seconds:g}s ({step.ease})")

    if timeline > MAX_SHOT_SECONDS:
        raise ValueError(f"shot is longer than {MAX_SHOT_SECONDS:.0f}s ({timeline:.0f}s)")

    shot.ticks = _merge_simultaneous(shot.ticks)
    shot.duration = timeline
    return shot


def _merge_simultaneous(ticks: list[Tick]) -> list[Tick]:
    """Merge ticks that land on the same instant so one timestamp carries all its axes.

    Steps that overlap in time (a diagonal, a figure-eight) each emit their own ticks; without
    this, the executor would write one axis, sleep out the shared period, and write the next --
    producing a stair-step instead of a diagonal.
    """
    merged: dict[float, dict[Axis, int]] = {}
    for tick in ticks:
        key = round(tick.t, 6)
        merged.setdefault(key, {}).update(tick.targets)
    return [Tick(t=t, targets=merged[t]) for t in sorted(merged)]


def recentre_steps(specs: dict[Axis, AxisSpec], seconds: float = 1.5) -> list[dict]:
    """Build the steps that return every axis to its default: the 'home' shot."""
    return [
        {"axis": axis.value, "to": spec.default, "seconds": seconds, "ease": "in_out"}
        for axis, spec in sorted(specs.items(), key=lambda item: item[0].value)
    ]
