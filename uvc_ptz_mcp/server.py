"""The MCP server: a camera's aim as an outcome, with every move confirmed from the picture.

Design rules this file enforces, each one from something measured on the reference hardware:

  * **A move is only reported as done when the picture says it moved.** The device's own report
    was measured lying in both directions, so `moved` comes from comparing frames, and the
    device's claim is returned beside it as `device_reported`, labelled as a hint.
  * **Success without movement is success.** Asking for the position the camera is already at
    returns `moved: false` and no error. Erroring would train a caller to retry pointlessly.
  * **A command the hardware did not follow is an error, not a quiet success.** After a bounded
    retry, an unapplied move raises ToolError naming what was requested and what was observed.
  * **Blocking work never runs on the event loop.** DirectShow COM calls and ffmpeg captures go
    through `asyncio.to_thread`; a blocked loop starves the very tasks that serve the host.
  * **Observation is explicit.** Nothing captures a frame except when a tool is called to.

The server starts and serves its tool list even with no camera present, because hosts list tools
before anyone knows whether the hardware is there -- and a directory evaluates a server by
starting it. A missing device is reported by `camera_status` and by the tools that need it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .backend import CameraBackend, open_backend
from .calibration import Calibration, load_calibration, save_calibration
from .camera import Axis, AxisSpec, parse_axis
from .frames import png_bytes
from .motion import compile_shot
from .tracking import (
    DEFAULT_DEAD_ZONE_PX,
    DEFAULT_GAIN_DEG_PER_PX,
    DEFAULT_MAX_STEP_DEGREES,
    FaceDetector,
    Tracker,
    run_loop,
)
from .verify import compare

_LOGGER = logging.getLogger("uvc_ptz_mcp")

MAX_MOVE_ATTEMPTS = 2
SETTLE_MARGIN = 1.15  # a little longer than the measured settle, since measurements are minima


@dataclass
class Session:
    """Everything the tools share: one camera, one calibration, one verification threshold."""

    backend: CameraBackend
    calibration: Calibration
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    marks: dict[str, np.ndarray] = field(default_factory=dict)
    start_error: str | None = None
    # A follow loop, if one is running. Kept on the session so the tools that start, inspect and
    # stop it all see the same one, and so it can be cancelled when the server goes away.
    tracker: Any = None
    track_task: asyncio.Task | None = None

    def cancel_tracking(self) -> None:
        """Stop a running follow loop, if there is one. Safe to call when there is not."""
        if self.tracker is not None:
            self.tracker.running = False
        if self.track_task is not None and not self.track_task.done():
            self.track_task.cancel()
        self.track_task = None

    @property
    def threshold(self) -> float:
        """The picture-difference threshold for this device."""
        return self.calibration.threshold

    @property
    def specs(self) -> dict[Axis, AxisSpec]:
        """The axes this camera exposes."""
        return self.backend.specs()

    @property
    def device(self) -> str:
        """A stable name for this device, used to key its calibration file."""
        described = self.backend.describe()
        return str(described.get("device") or described.get("backend") or "camera")

    # -- primitives --------------------------------------------------------
    async def frame(self) -> np.ndarray:
        """One frame, off the event loop."""
        try:
            return await asyncio.to_thread(self.backend.frame)
        except Exception as error:  # noqa: BLE001 - surfaced with context by the caller
            raise ToolError(
                f"cannot read a frame from the camera, so nothing can be verified: {error}"
            ) from error

    async def reported(self, axis: Axis) -> int | None:
        """Return the device's own value for an axis. A hint; never used as proof."""
        return await asyncio.to_thread(self.backend.read, axis)

    async def settle(self, axis: Axis) -> float:
        """Seconds to wait for a move on this axis before looking."""
        return (await asyncio.to_thread(self.backend.settle_seconds, axis)) * SETTLE_MARGIN

    def spec_or_fail(self, axis: Axis) -> AxisSpec:
        """Return the spec for an axis, or raise naming the axes this camera does have."""
        spec = self.specs.get(axis)
        if spec is None:
            available = ", ".join(sorted(a.value for a in self.specs)) or "none"
            raise ToolError(f"this camera has no {axis.value} axis (it exposes: {available})")
        return spec

    async def send(self, axis: Axis, target: int) -> None:
        """Write an absolute target off the event loop."""
        try:
            await asyncio.to_thread(self.backend.write, axis, target)
        except Exception as error:  # noqa: BLE001 - a refused write is a real failure
            raise ToolError(f"the camera refused a write to {axis.value}: {error}") from error


def _move_result(  # noqa: PLR0917 - a result record; every field is named at the call site
    session: Session,
    axis: Axis,
    requested: int,
    reported_before: int | None,
    reported_after: int | None,
    verdict: Any,
    attempts: int,
    moved: bool,
) -> dict:
    """Assemble the standard answer for a move: what was asked, what moved, what proves it."""
    described = session.backend.describe()
    return {
        "axis": axis.value,
        "requested": requested,
        "moved": moved,
        "attempts": attempts,
        "device_reported": {"before": reported_before, "after": reported_after},
        "picture": verdict.to_dict() if verdict else None,
        "confirmed_by": "picture" if verdict else "not confirmed",
        "simulated": bool(described.get("simulated")),
        "unit": session.specs[axis].unit,
    }


async def _apply_axis(session: Session, axis: Axis, target: int) -> dict:
    """Move one axis and confirm it from the picture, retrying a dropped write once."""
    spec = session.spec_or_fail(axis)
    clamped = spec.clamp(target)
    tolerance = max(1, spec.step)
    async with session.lock:
        reported_before = await session.reported(axis)
        before = await session.frame()
        settle = await session.settle(axis)
        # The legitimate "no movement needed" case: the device already reports the target.
        already_there = reported_before is not None and abs(reported_before - clamped) <= tolerance

        verdict = None
        moved = False
        attempts = 0
        for _ in range(MAX_MOVE_ATTEMPTS):
            attempts += 1
            await session.send(axis, clamped)
            await asyncio.sleep(settle)
            after = await session.frame()
            verdict = compare(before, after, session.threshold, detail=f"{axis.value} move")
            if verdict.changed:
                moved = True
                break
            if already_there:
                break

        reported_after = await session.reported(axis)
        result = _move_result(
            session, axis, clamped, reported_before, reported_after, verdict, attempts, moved
        )
        if moved or already_there:
            return result

        raise ToolError(
            f"{axis.value} was commanded to {clamped} and the picture did not change "
            f"({attempts} attempt(s), difference {verdict.value:.3f} against a threshold of "
            f"{session.threshold:.3f}). The device reported {reported_after}. Read-back is "
            f"known to be unreliable on this hardware, so this is reported as a failure rather "
            f"than assumed to have worked."
        )


async def _drive_ticks(
    session: Session,
    ticks: list[Any],
    elapsed: float,
    step_number: int,
    label: str,
) -> float:
    """Write every axis of every tick, pacing to each tick's time.

    Returns the new elapsed time. A refused write aborts the shot here, naming the step,
    rather than continuing to drive a camera that has stopped listening.
    """
    for tick in ticks:
        wait = tick.t - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        for axis, value in tick.targets.items():
            try:
                await asyncio.to_thread(session.backend.write, axis, value)
            except Exception as error:  # noqa: BLE001
                raise ToolError(
                    f"shot aborted at step {step_number} ({label}): "
                    f"a write to {axis.value} failed: {error}"
                ) from error
        elapsed = tick.t
    return elapsed


async def _apply_shot(session: Session, raw_steps: list[dict]) -> dict:
    """Run a multi-step shot, verifying after every waypoint."""
    async with session.lock:
        specs = session.specs
        if not specs:
            raise ToolError("no camera axes are available, so a shot cannot be compiled")

        pose_hints = {}
        for axis in specs:
            value = await session.reported(axis)
            pose_hints[axis] = specs[axis].default if value is None else int(value)

        try:
            shot = compile_shot(raw_steps, specs, pose_hints)
        except ValueError as error:
            raise ToolError(str(error)) from None

        before = await session.frame()
        previous_frame = before
        report: list[dict] = []
        elapsed = 0.0

        for index, step_end in enumerate(shot.step_ends):
            ticks = [tick for tick in shot.ticks if elapsed < tick.t <= step_end + 1e-9]
            elapsed = await _drive_ticks(
                session, ticks, elapsed, index + 1, shot.step_labels[index]
            )

            # Wait for every axis this step drove, using the slowest one's measured settle time.
            step_axes = {axis for tick in ticks for axis in tick.targets}
            if step_axes:
                settles = [await session.settle(axis) for axis in sorted(step_axes, key=str)]
                await asyncio.sleep(max(settles))
            after = await session.frame()
            verdict = compare(previous_frame, after, session.threshold)
            step = {
                "step": index + 1,
                "label": shot.step_labels[index],
                "picture_difference": round(verdict.value, 4),
                "view_changed": verdict.changed,
            }
            report.append(step)
            previous_frame = after
            elapsed = step_end
            if not verdict.changed and index < len(shot.step_ends) - 1:
                # An intermediate waypoint that changed nothing is suspicious but not fatal: the
                # next step may still move the camera. Recorded, not raised.
                step["note"] = "no visible change at this waypoint"

        return {
            "plan": shot.to_dict(),
            "steps": report,
            "moved_from_start": compare(before, previous_frame, session.threshold).to_dict(),
            "simulated": bool(session.backend.describe().get("simulated")),
        }


def _tracking_move(session: Session) -> Callable[[float, float], Awaitable[dict]]:
    """Build the move the follow loop uses: a bounded, picture-verified pan/tilt nudge.

    Each axis is applied on its own so each one is confirmed by the picture, which is the only
    confirmation available: the loop's whole premise is that the device's report says nothing
    useful. An axis already at the end of its travel reports no movement rather than pretending.
    """

    async def move(pan_degrees: float, tilt_degrees: float) -> dict:
        reports: dict[str, dict] = {}
        for axis, step in ((Axis.PAN, pan_degrees), (Axis.TILT, tilt_degrees)):
            spec = session.specs.get(axis)
            if spec is None or not step:
                continue
            reported = await session.reported(axis)
            base = spec.default if reported is None else int(reported)
            target = spec.clamp(base + step)
            if target == base:
                reports[axis.value] = {
                    "moved": False,
                    "detail": "already at the end of its travel",
                }
                continue
            reports[axis.value] = await _apply_axis(session, axis, target)
        return {
            "moved": any(bool(report.get("moved")) for report in reports.values()),
            "confirmed_by": "picture" if reports else None,
            "axes": reports,
        }

    return move


def build_server(session: Session) -> MCPServer:  # noqa: C901, PLR0915
    """Create the MCP server bound to one session.

    Deliberately one function: every tool's description is the only instruction an agent
    receives, so keeping each description beside the tool it describes is worth the length.
    """
    server = MCPServer(
        name="uvc-ptz-camera-mcp",
        version=__version__,
        instructions=(
            "Control a USB (UVC) pan/tilt/zoom camera: aim it, nudge it, sweep it smoothly, "
            "zoom, and look through it. Every move is confirmed by comparing the picture before "
            "and after, because this class of camera reports positions it never moved to. "
            "Snapshot and video tools observe the camera's view, so call them when the user asks."
        ),
    )

    @server.tool()
    async def camera_status() -> dict:
        """Report the camera, its axes and ranges, and whether it is a real device.

        Call this first. It says which camera was found, the range each axis accepts, where the
        camera believes it is pointing (a hint, not proof), what has been calibrated, and -- if
        no real device was found -- the reason. `simulated: true` in any result means nothing
        physical moved and no picture was ever taken.
        """
        described = session.backend.describe()
        reported: dict[str, Any] = {}
        for axis in session.specs:
            reported[axis.value] = await session.reported(axis)
        return {
            "backend": described,
            "axes": {axis.value: spec.to_dict() for axis, spec in session.specs.items()},
            "device_reported_position": reported,
            "verification": {
                "method": "z-normalised frame difference, threshold calibrated on labelled "
                "hardware frames",
                "threshold": session.threshold,
                "marks": sorted(session.marks),
            },
            "calibration": {
                "device": session.calibration.device,
                "neutral": session.calibration.neutral,
                "aims": sorted(session.calibration.aims),
                "notes": session.calibration.notes,
            },
            "start_error": session.start_error,
        }

    @server.tool()
    async def aim(axis: str, to: int) -> dict:
        """Point one axis at an absolute value, and return what the picture confirms.

        `axis` is one of pan, tilt, roll, zoom. Gimbal axes are in degrees; zoom is the device's
        own ratio x100, so 300 means 3x. The value is clamped to the range the camera advertises
        and the clamped value is what gets sent.

        This moves the camera and waits for it: allow roughly a second for a gimbal axis and
        three for a zoom. `moved` is decided by comparing frames, never by the device's report --
        the report is returned as `device_reported` and labelled a hint, because this hardware
        was measured claiming positions it had not reached. If the picture shows no change and
        the camera was not already at the target, the call fails rather than reporting success.
        """
        session.spec_or_fail(parse_axis(axis))
        return await _apply_axis(session, parse_axis(axis), int(to))

    @server.tool()
    async def nudge(axis: str, by: int) -> dict:
        """Move one axis relative to where the device says it is now.

        Relative moves inherit the device's reported position, which is a hint: after a series
        of nudges the accumulated position may drift from belief. The picture check still tells
        you whether *this* call moved the camera; it cannot tell you it is at an absolute angle.
        """
        parsed = parse_axis(axis)
        spec = session.spec_or_fail(parsed)
        current = await session.reported(parsed)
        origin = spec.default if current is None else int(current)
        return await _apply_axis(session, parsed, origin + int(by))

    @server.tool()
    async def sweep(axis: str, to: int, seconds: float = 2.0, ease: str = "in_out") -> dict:
        """Move an axis smoothly to a value over a chosen duration.

        Use this for any move that should read as deliberate: a slow pan across a scene, a
        gradual push in. The axis is driven by streaming absolute targets at 15 Hz rather than
        one jump, which is how this hardware was measured moving smoothly. `ease` is one of
        linear, in_out, out, in; in_out starts and stops gently, which is what makes a move look
        intentional. Long sweeps take as long as `seconds`, so expect to wait.
        """
        parsed = parse_axis(axis)
        spec = session.spec_or_fail(parsed)
        current = await session.reported(parsed)
        origin = spec.default if current is None else int(current)
        target = spec.clamp(to)

        shot = compile_shot(
            [{"axis": parsed.value, "to": target, "seconds": float(seconds), "ease": str(ease)}],
            session.specs,
            {parsed: origin},
        )
        async with session.lock:
            before = await session.frame()
            elapsed = 0.0
            for tick in shot.ticks:
                wait = tick.t - elapsed
                if wait > 0:
                    await asyncio.sleep(wait)
                for tick_axis, value in tick.targets.items():
                    await asyncio.to_thread(session.backend.write, tick_axis, value)
                elapsed = tick.t
            await asyncio.sleep(await session.settle(parsed))
            after = await session.frame()
            verdict = compare(before, after, session.threshold, detail=f"seek {axis}")
            reported_after = await session.reported(parsed)

        return {
            "axis": parsed.value,
            "requested": target,
            "duration_seconds": round(shot.duration, 3),
            "ticks": len(shot.ticks),
            "moved": verdict.changed,
            "device_reported": reported_after,
            "picture": verdict.to_dict(),
            "warnings": shot.warnings,
            "simulated": bool(session.backend.describe().get("simulated")),
        }

    @server.tool()
    async def zoom(ratio: float) -> dict:
        """Set optical zoom as a multiplier: 1.0 is wide, up to the camera's maximum.

        Convenience over the raw zoom axis, which counts in hundredths. The requested ratio is
        clamped to what the camera supports and the clamped value is returned.
        """
        spec = session.spec_or_fail(Axis.ZOOM)
        target = spec.clamp(ratio * 100)
        return await _apply_axis(session, Axis.ZOOM, target)

    @server.tool()
    async def recentre() -> dict:
        """Return every axis to the camera's own default: centre, level, unzoomed."""
        reports = []
        for axis, spec in sorted(session.specs.items(), key=lambda item: item[0].value):
            try:
                reports.append(await _apply_axis(session, axis, spec.default))
            except ToolError as error:
                reports.append({"axis": axis.value, "error": str(error)})
        return {"recentred": reports}

    @server.tool()
    async def look() -> Image:
        """Capture one frame of what the camera currently sees, as a PNG image.

        This observes the camera's view. Call it when the user asks to see the picture, not on
        your own initiative, and be aware that a still only tells you what is in front of the
        lens -- not where the camera is, and not whether a move happened.
        """
        frame = await session.frame()
        return Image(data=png_bytes(frame), format="png")

    @server.tool()
    async def aim_learn(label: str) -> dict:
        """Remember the current direction under a name, for later use.

        Pan is an absolute axis whose meaning depends on where the camera is standing, so
        "point at the door" is unanswerable without recording it once. Call this with the camera
        already pointed where you want, and the label becomes usable with `go_to`.
        """
        pose: dict[Axis, int] = {}
        for axis in session.specs:
            value = await session.reported(axis)
            pose[axis] = int(value) if value is not None else session.specs[axis].default
        session.calibration.remember_aim(label, pose)
        path = save_calibration(session.calibration)
        return {
            "label": label,
            "recorded": {axis.value: value for axis, value in pose.items()},
            "stored_at": str(path),
            "caveat": "these are the device's reported values, which are a hint; re-check with "
            "a snapshot if the label matters",
        }

    @server.tool()
    async def aim_list() -> dict:
        """List the directions recorded for this camera, and its calibrated default pose."""
        return {
            "device": session.calibration.device,
            "neutral": session.calibration.neutral,
            "aims": session.calibration.aims,
            "threshold": session.threshold,
        }

    @server.tool()
    async def go_to(label: str) -> dict:
        """Point the camera at a direction recorded earlier with `aim_learn`.

        Each axis is moved and confirmed from the picture, the same as `aim`. A label is resolved
        to the angles recorded for it, which may drift if the camera has been physically moved
        since; the picture check still proves that each axis moved, not that the label is right.
        """
        try:
            pose = session.calibration.aim(label)
        except KeyError as error:
            raise ToolError(str(error)) from None
        reports = []
        for axis_name, value in pose.items():
            try:
                axis = parse_axis(axis_name)
            except ValueError:
                continue
            if axis not in session.specs:
                continue
            try:
                reports.append(await _apply_axis(session, axis, int(value)))
            except ToolError as error:
                reports.append({"axis": axis_name, "error": str(error)})
        return {"label": label, "target": pose, "moved": reports}

    @server.tool()
    async def plan_shot(steps: list[dict]) -> dict:
        """Compile a shot and report what it would do, without moving the camera.

        Takes exactly the step list `run_shot` takes: each step is an object like
        {"axis": "pan", "to": 60, "seconds": 2.5, "ease": "in_out"}, optionally with
        {"hold": 1.0} to pause after it. Every step is validated against the camera's advertised
        ranges and clamped exactly as it would be at execution time, so an impossible or
        out-of-range shot is refused here, before anything moves.

        Use this to compose a shot. It returns the per-step schedule (where each axis starts, the
        value it was clamped to, how far it travels, how many ticks it costs), the per-axis
        envelope, the total duration, and the axes with their ranges so the next attempt can be
        written against the real limits. Nothing is written to the camera and no frame is captured,
        so it is safe to call as often as you like while deciding.

        The `from` values come from the device's own position report, which this server treats as a
        hint rather than a fact. `run_shot` recompiles from the same hint when it runs, so a plan
        and its execution agree unless the camera moved in between.
        """
        if not isinstance(steps, list) or not steps:
            raise ToolError("steps must be a non-empty list of step objects")
        specs = session.specs
        if not specs:
            raise ToolError("no camera axes are available, so a shot cannot be compiled")

        pose_hints = {}
        for axis in specs:
            reported = await session.reported(axis)
            pose_hints[axis] = specs[axis].default if reported is None else int(reported)

        try:
            shot = compile_shot(steps, specs, pose_hints)
        except ValueError as error:
            raise ToolError(str(error)) from None

        return {
            "plan": shot.to_dict(),
            "envelope": shot.envelope(),
            "axes": [spec.to_dict() for spec in specs.values()],
            "totals": {
                "duration_seconds": round(shot.duration, 3),
                "ticks": len(shot.ticks),
                "steps": len(shot.compiled),
            },
            "moved": False,
            "note": "nothing was moved; pass the same steps to run_shot to execute this",
        }

    @server.tool()
    async def run_shot(steps: list[dict]) -> dict:
        """Execute a multi-step camera move, verifying after every waypoint.

        Each step is an object: {"axis": "pan", "to": 60, "seconds": 2.5, "ease": "in_out"}.
        Steps run in sequence and every axis that moves in a step is written on each tick, so a
        step naming two axes produces a genuine diagonal rather than a staircase. Add
        {"hold": 1.0} to pause after a step.

        The whole plan is validated against the camera's advertised ranges before anything moves,
        so an impossible shot fails without touching the hardware. Afterwards the report gives the
        picture difference at each waypoint, which is how you tell a shot that happened from one
        that did not. A long shot takes its full duration; do not set a host timeout shorter.
        """
        if not isinstance(steps, list) or not steps:
            raise ToolError("steps must be a non-empty list of step objects")
        return await _apply_shot(session, steps)

    @server.tool()
    async def track_start(
        target: str = "face",
        gain: float = DEFAULT_GAIN_DEG_PER_PX,
        dead_zone_px: int = DEFAULT_DEAD_ZONE_PX,
        invert: bool = False,
        max_seconds: float = 120.0,
    ) -> dict:
        """Start following a subject, closing the loop on the picture.

        While this runs the server keeps capturing frames and moving the camera to hold the subject
        near the centre of the frame: detect, measure the offset, take a bounded step, look again.
        It decides only from the picture, because this class of camera's position report cannot be
        trusted -- and it reports the residual offset every iteration, so a loop that is losing the
        subject looks like one instead of seeming to work.

        It looks at whatever the camera sees for as long as it runs, which may be a room with people
        in it. Start it when the user wants the camera following something, and stop it when they do
        not. It also stops by itself: when the subject is lost for a while, when it settles inside
        the dead zone, or after `max_seconds`.

        `gain` is degrees of camera movement per pixel of offset -- a starting point, not a
        measurement. `invert` flips the direction, which is what a differently mounted camera needs:
        if the camera drives *away* from the subject, the sign is wrong for this mount, so read
        `track_status` and start again with invert true. Movement is capped at 25 degrees per
        iteration, so a badly wrong gain converges slowly rather than throwing the camera across
        its range.
        """
        if session.track_task is not None and not session.track_task.done():
            raise ToolError("a follow loop is already running; stop it with track_stop first")
        wanted = target.strip().lower()
        if wanted != "face":
            raise ToolError(f"unknown target {target!r}; this build can follow 'face'")
        if not session.specs:
            raise ToolError("no camera axes are available, so nothing can be followed")

        try:
            detector = FaceDetector()
        except RuntimeError as error:
            raise ToolError(str(error)) from None

        tracker = Tracker(
            target=wanted,
            gain=gain,
            dead_zone_px=dead_zone_px,
            max_step=DEFAULT_MAX_STEP_DEGREES,
            invert=invert,
            max_seconds=max_seconds,
        )
        session.tracker = tracker
        session.track_task = asyncio.create_task(
            run_loop(
                tracker,
                detector=detector,
                capture=session.frame,
                move=_tracking_move(session),
                sleep=asyncio.sleep,
            )
        )
        return {
            "started": True,
            "moved": None,
            "note": (
                "the camera is being looked through and moved while this runs; "
                "call track_status to watch it and track_stop to end it"
            ),
            "tracker": tracker.to_dict(),
        }

    @server.tool()
    async def track_status() -> dict:
        """Report what the follow loop is doing, with the evidence for each iteration.

        Every iteration appears with the measured offset and the step applied, so a loop that is
        driving the wrong way shows up as a residual distance that grows, and one whose moves the
        picture never confirms shows up in `move_failures` rather than as apparent success.
        """
        if session.tracker is None:
            return {"running": False, "note": "no follow loop has been started on this session"}
        return session.tracker.to_dict()

    @server.tool()
    async def track_stop() -> dict:
        """Stop following, and return the summary: what it saw, what it moved, what failed."""
        if session.tracker is None:
            raise ToolError("no follow loop has been started on this session")
        tracker = session.tracker
        tracker.running = False
        task = session.track_task
        if task is not None and not task.done():
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                # Let the current iteration finish so the summary includes it, but do not wait
                # forever for a camera call that has stopped answering.
                await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
            if not task.done():
                task.cancel()
        session.track_task = None
        return {"stopped": True, "tracker": tracker.to_dict()}

    @server.tool()
    async def mark_view(label: str) -> dict:
        """Store the current picture under a label, so it can be compared later.

        Useful before a move whose result you will judge later, or to establish what "the wide
        shot" looks like. Marks live in memory for this session only.
        """
        frame = await session.frame()
        session.marks[str(label)] = frame
        return {
            "label": str(label),
            "marks": sorted(session.marks),
            "threshold": session.threshold,
            "frame_shape": list(frame.shape),
        }

    @server.tool()
    async def check_view(label: str) -> dict:
        """Compare the current picture with one stored by `mark_view`.

        Answers "is the camera still looking at what it was looking at?", which is the question
        a scene change can otherwise hide: a person walking through the shot changes the picture
        as much as a pan does, so treat a small difference as "still there" and a large one as
        "worth looking at".
        """
        if label not in session.marks:
            known = ", ".join(sorted(session.marks)) or "none"
            raise ToolError(f"no view marked {label!r}; marked views: {known}")
        frame = await session.frame()
        verdict = compare(session.marks[label], frame, session.threshold, detail=f"view {label}")
        return {"label": label, **verdict.to_dict()}

    return server


def open_session(backend_kind: str = "auto", device: str | None = None) -> Session:
    """Open a backend and its calibration, without ever raising.

    "No camera" is a normal state for this server: a host lists tools before it knows what is
    plugged in, and a directory evaluates a server by starting it. So a missing device becomes a
    recorded reason -- surfaced by `camera_status` and by every tool that needs hardware -- and
    the simulator stands in, clearly labelled, rather than the process dying.
    """
    try:
        backend = open_backend(backend_kind, device)
        if not getattr(backend, "_opened", True):
            backend.open()
    except Exception as error:  # noqa: BLE001 - any failure is reported, not fatal
        from .simulator import SimulatorBackend  # noqa: PLC0415 - only needed on the failure path

        backend = SimulatorBackend()
        backend.fallback_reason = f"{type(error).__name__}: {error}"
        backend.open()
        _LOGGER.warning("no camera available: %s", error)

    described = backend.describe()
    stands_in = bool(described.get("simulated")) and bool(described.get("stand_in_for_hardware"))
    key = str(described.get("device") or described.get("backend") or "camera")
    return Session(
        backend=backend,
        calibration=load_calibration(key),
        start_error=(
            f"no camera was available, so results are simulated: {described.get('reason')}"
            if stands_in
            else None
        ),
    )


def configure_logging() -> None:
    """Send logs to stderr: stdout is the JSON-RPC channel and a stray write corrupts it."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="uvc-ptz-camera-mcp: %(levelname)s %(message)s",
    )


def main_server(backend_kind: str = "auto", device: str | None = None) -> None:
    """Open the camera, build the server, and serve over stdio until the host closes it."""
    configure_logging()
    session = open_session(backend_kind, device)
    server = build_server(session)
    try:
        server.run()
    finally:
        # A follow loop still running when the server goes away must not keep touching the camera.
        session.cancel_tracking()
        with contextlib.suppress(Exception):
            session.backend.close()
