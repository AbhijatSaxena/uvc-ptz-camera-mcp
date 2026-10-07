"""Follow mode: the control law, the loop, and the wiring through the tools.

Three levels, deliberately. The arithmetic is tested as arithmetic; the loop is tested with
injected capture/move/sleep so the iteration behaviour (settle, lose the subject, give up when the
camera stops answering) is deterministic; and the tools are tested against the simulated camera so
the loop is shown to go through the same picture-verified move path as `aim`, rather than its own
unverified shortcut.

`track_start` needs a detector, and the real one needs OpenCV. These tests substitute a scripted
detector so they run anywhere, and one test asserts that the *absence* of OpenCV produces a tool
error that names the install rather than an obscure import failure.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.test_tools import SPEED, call, make_session
from uvc_ptz_mcp import server as server_module
from uvc_ptz_mcp.server import build_server
from uvc_ptz_mcp.simulator import SimulatorBackend
from uvc_ptz_mcp.tracking import (
    Offset,
    Target,
    Tracker,
    offset_from,
    run_loop,
    step_for,
)

FRAME = np.zeros((180, 320), dtype=np.uint8)  # centre is (160, 90)


# -- the arithmetic --------------------------------------------------------------------


def test_a_target_inside_the_dead_zone_is_left_alone():
    """Hunting for the last few pixels is how a camera ends up jittering forever."""
    assert step_for(Offset(dx=10, dy=-8), dead_zone_px=24) == (0.0, 0.0)
    assert step_for(Offset(dx=0, dy=0)) == (0.0, 0.0)


def test_a_target_outside_the_dead_zone_moves_proportionally_and_bounded():
    pan, tilt = step_for(Offset(dx=40, dy=-40), gain=0.5, dead_zone_px=10, max_step=25)
    assert (pan, tilt) == (20.0, -20.0)

    # A wrong gain must not be able to throw the camera across its range in one command.
    pan, tilt = step_for(Offset(dx=500, dy=500), gain=1.0, dead_zone_px=10, max_step=25)
    assert (pan, tilt) == (25.0, 25.0)


def test_invert_flips_the_direction():
    """A different mount answers the axes the other way round; the loop cannot know."""
    normal = step_for(Offset(dx=40, dy=-40), gain=0.5, dead_zone_px=10)
    inverted = step_for(Offset(dx=40, dy=-40), gain=0.5, dead_zone_px=10, invert=True)
    assert inverted == (-normal[0], -normal[1])


def test_offset_is_measured_from_the_centre_of_the_frame():
    right = offset_from(Target(label="face", x=300, y=90), FRAME)
    assert right.dx == pytest.approx(140.0)
    assert right.dy == pytest.approx(0.0)
    assert right.distance == pytest.approx(140.0)
    centre = offset_from(Target(label="face", x=160, y=90), FRAME)
    assert centre.distance == pytest.approx(0.0)


# -- the loop, with everything injected ------------------------------------------------


class ScriptedDetector:
    """A detector that returns a fixed sequence, so the loop's behaviour is deterministic."""

    def __init__(self, script: list[Target | None], on_call=None):
        """Hold the script; `on_call` runs each time, for tests that stop the loop mid-flight."""
        self.script = list(script)
        self.on_call = on_call
        self.calls = 0

    def locate(self, frame):
        """Return the next scripted finding, repeating the last one when the script runs out."""
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


async def no_sleep(_seconds: float) -> None:
    """Skip the loop's pacing; the timing is not what these tests are about."""


async def frame_ok():
    """Return a frame that never changes: the loop's behaviour is what is under test here."""
    return FRAME


class Recorder:
    """Records the moves the loop asked for, and can be told how to answer."""

    def __init__(self, answer: dict | None = None, raises: Exception | None = None):
        """Set the canned answer, or the exception to raise."""
        self.calls: list[tuple[float, float]] = []
        self.answer = answer or {"moved": True, "confirmed_by": "picture"}
        self.raises = raises

    async def __call__(self, pan: float, tilt: float) -> dict:
        """Record the request and answer it."""
        self.calls.append((pan, tilt))
        if self.raises is not None:
            raise self.raises
        return self.answer


def target_at(x: float, y: float = 90.0) -> Target:
    """Build a finding at a pixel position."""
    return Target(label="face", x=x, y=y)


async def test_the_loop_settles_when_the_subject_is_centred():
    moves = Recorder()
    tracker = Tracker()
    await run_loop(
        tracker,
        detector=ScriptedDetector([target_at(160)]),
        capture=frame_ok,
        move=moves,
        sleep=no_sleep,
    )
    assert moves.calls == [], "nothing to do when the subject is already centred"
    assert tracker.stopped_because == "the subject is inside the dead zone"
    assert tracker.to_dict()["converged"] is True


async def test_the_loop_steps_toward_the_subject_then_settles():
    moves = Recorder()
    tracker = Tracker(dead_zone_px=24)
    await run_loop(
        tracker,
        detector=ScriptedDetector([target_at(320), target_at(320), target_at(160)]),
        capture=frame_ok,
        move=moves,
        sleep=no_sleep,
    )
    assert len(moves.calls) == 2, "one move per iteration while the subject is off-centre"
    assert all(pan > 0 for pan, _ in moves.calls), "a subject to the right moves the axis positive"
    assert all(pan == pytest.approx(25.0) for pan, _ in moves.calls), "bounded per iteration"
    assert tracker.moves == 2
    assert tracker.stopped_because == "the subject is inside the dead zone"
    assert [entry["moved"] for entry in tracker.history[:2]] == [True, True]
    assert tracker.history[-1]["settled"] is True, "the last iteration settled: no move was asked"


async def test_the_loop_gives_up_when_it_loses_the_subject():
    moves = Recorder()
    tracker = Tracker(lost_after=3)
    await run_loop(
        tracker,
        detector=ScriptedDetector([None]),
        capture=frame_ok,
        move=moves,
        sleep=no_sleep,
    )
    assert moves.calls == []
    assert tracker.lost_frames == 3
    assert "lost the target" in tracker.stopped_because
    assert tracker.to_dict()["converged"] is False


async def test_the_loop_stops_when_the_camera_stops_answering():
    """Unconfirmed moves are counted, not thrown: a loop must not die on the first hiccup."""
    moves = Recorder(answer={"moved": False, "confirmed_by": "picture", "axes": {}})
    tracker = Tracker(max_failures=4, dead_zone_px=1)
    await run_loop(
        tracker,
        detector=ScriptedDetector([target_at(320)]),
        capture=frame_ok,
        move=moves,
        sleep=no_sleep,
    )
    assert len(moves.calls) == 4
    assert tracker.consecutive_failures == 4
    assert "did not move the camera" in tracker.stopped_because
    assert all(entry.get("unconfirmed") for entry in tracker.history)


async def test_a_move_that_raises_is_recorded_not_propagated():
    tracker = Tracker(max_failures=2, dead_zone_px=1)
    moves = Recorder(raises=RuntimeError("the write was refused"))
    await run_loop(
        tracker,
        detector=ScriptedDetector([target_at(320)]),
        capture=frame_ok,
        move=moves,
        sleep=no_sleep,
    )
    assert tracker.move_failures == 2
    assert "the write was refused" in tracker.history[0]["move_failed"]


async def test_the_loop_stops_at_its_time_limit():
    """A follow loop that can run unnoticed forever is a camera nobody is watching."""
    tracker = Tracker(max_seconds=10.0, dead_zone_px=1)
    readings = {"count": 0}

    def clock() -> float:
        """Report the loop's start, then a time past the limit."""
        readings["count"] += 1
        return tracker.started_at + (0.0 if readings["count"] == 1 else 100.0)

    await run_loop(
        tracker,
        detector=ScriptedDetector([target_at(320)]),
        capture=frame_ok,
        move=Recorder(),
        sleep=no_sleep,
        now=clock,
    )
    assert "max_seconds" in tracker.stopped_because
    assert tracker.moves == 1, "it did one iteration's work and then stopped"


async def test_the_loop_stops_when_asked_from_inside():
    tracker = Tracker(dead_zone_px=1)

    def stop_it():
        tracker.running = False if tracker.iterations else tracker.running

    await run_loop(
        tracker,
        detector=ScriptedDetector([target_at(320)], on_call=stop_it),
        capture=frame_ok,
        move=Recorder(),
        sleep=no_sleep,
    )
    assert tracker.stopped_because in {
        "stopped on request",
        "the subject is inside the dead zone",
    }


# -- through the tools, against the simulated camera ------------------------------------


class FakeFaceDetector:
    """Stands in for the OpenCV detector: returns a scripted finding, repeated."""

    def __init__(self, finding: Target | None = None, script: list | None = None):
        """Hold the finding(s) to report."""
        self.script = list(script) if script else [finding or target_at(320)]
        self.calls = 0

    def locate(self, frame):
        """Report the next finding."""
        self.calls += 1
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


def use_detector(monkeypatch, detector) -> None:
    """Make the server build the given detector instead of the OpenCV one."""
    monkeypatch.setattr(server_module, "FaceDetector", lambda: detector)


async def test_track_start_moves_the_camera_and_reports_it_honestly(monkeypatch, tmp_path):
    """The loop must go through the verified move path, not its own shortcut."""
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    backend = SimulatorBackend(speed=SPEED, drop_probability=0.0)
    backend.open()
    session = make_session(backend, tmp_path)
    server = build_server(session)
    use_detector(
        monkeypatch,
        FakeFaceDetector(script=[target_at(320), target_at(320), target_at(160)]),
    )

    started = await call(server, "track_start", {"dead_zone_px": 24})
    assert started["started"] is True
    assert started["moved"] is None, "starting is not a claim that anything moved"

    for _ in range(200):  # let the background loop run
        if session.track_task is not None and session.track_task.done():
            break
        await asyncio.sleep(0.05)

    assert backend.actions > 0, "the loop should have written to the camera"
    status = await call(server, "track_status")
    assert status["moves"] >= 1
    assert status["history"], "every iteration is reported"
    assert status["history"][0]["confirmed_by_picture"] == "picture", (
        "the follow loop inherits the picture-verified move path"
    )

    summary = await call(server, "track_stop")
    assert summary["stopped"] is True
    assert summary["tracker"]["stopped_because"]


async def test_track_start_settles_without_moving_a_centred_subject(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    backend = SimulatorBackend(speed=SPEED, drop_probability=0.0)
    backend.open()
    session = make_session(backend, tmp_path)
    server = build_server(session)
    use_detector(monkeypatch, FakeFaceDetector(target_at(160)))

    await call(server, "track_start")
    for _ in range(200):
        if session.track_task is not None and session.track_task.done():
            break
        await asyncio.sleep(0.05)

    assert backend.actions == 0, "a centred subject needs no movement at all"
    status = await call(server, "track_status")
    assert status["converged"] is True
    assert status["moves"] == 0


async def test_track_status_before_any_start_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    server = build_server(make_session(tmp_path=tmp_path))
    status = await call(server, "track_status")
    assert status["running"] is False
    assert "no follow loop" in status["note"]


async def test_track_stop_without_a_loop_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    server = build_server(make_session(tmp_path=tmp_path))
    with pytest.raises(ToolError, match="no follow loop"):
        await call(server, "track_stop")


async def test_an_unsupported_target_is_refused_by_name(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    server = build_server(make_session(tmp_path=tmp_path))
    with pytest.raises(ToolError, match="can follow 'face'"):
        await call(server, "track_start", {"target": "a dog"})


async def test_without_opencv_the_error_names_the_install(monkeypatch, tmp_path):
    """An optional extra that is missing must say what to install, not fail obscurely."""
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    server = build_server(make_session(tmp_path=tmp_path))

    def missing():
        raise RuntimeError("following needs OpenCV: pip install 'uvc-ptz-camera-mcp[tracking]'")

    monkeypatch.setattr(server_module, "FaceDetector", missing)
    with pytest.raises(ToolError, match=r"\[tracking\]"):
        await call(server, "track_start")
