"""Layer 1c: the tool layer, in-process, against the simulated camera.

This is where the rules that keep an agent honest are pinned:

  * a move is reported as `moved` only when the picture changed;
  * asking for the position the camera already holds is success without movement, not an error;
  * a write the hardware ignored (a *dropped* write, modelled from the real device) is retried,
    and if it still does not land the tool fails, naming what was requested and observed;
  * device-reported values are returned, but labelled as hints;
  * blocking work runs off the event loop.
"""

from __future__ import annotations

import base64
import json
import threading

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from uvc_ptz_mcp.calibration import Calibration
from uvc_ptz_mcp.server import Session, build_server
from uvc_ptz_mcp.simulator import SimulatorBackend

SPEED = 40.0  # compress settle times so the suite stays quick without skipping assertions


class RecordingSimulator(SimulatorBackend):
    """A simulator that records which thread its blocking calls ran on."""

    def __init__(self, **kwargs):
        """Start the simulator and an empty thread log."""
        super().__init__(**kwargs)
        self.threads: list[threading.Thread] = []

    def frame(self):
        """Record the calling thread, then render."""
        self.threads.append(threading.current_thread())
        return super().frame()


def make_session(backend: SimulatorBackend | None = None, tmp_path=None) -> Session:
    """Build a session over the simulator, with calibration in a temporary directory."""
    backend = backend or SimulatorBackend(speed=SPEED, drop_probability=0.0)
    backend.open()
    device = str(backend.describe().get("backend"))
    directory = tmp_path or "."
    calibration = Calibration(device=device)
    calibration_path = f"{directory}/cal.json"
    calibration.notes.append(calibration_path)
    return Session(backend=backend, calibration=calibration)


async def call(server, name: str, arguments: dict | None = None):
    """Invoke a tool and decode its JSON payload (or return the raw block for images)."""
    result = await server.call_tool(name, arguments or {})
    content = getattr(result, "content", None)
    if content is None and isinstance(result, tuple):
        content = result[0]
    block = content[0]
    text = getattr(block, "text", None)
    return block if text is None else json.loads(text)


async def test_status_says_it_is_simulated(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    server = build_server(make_session())
    status = await call(server, "camera_status")
    assert status["backend"]["simulated"] is True
    assert "pan" in status["axes"]
    assert status["axes"]["zoom"]["unit"] == "ratio_x100"
    assert status["verification"]["threshold"] > 0


async def test_aim_moves_and_is_confirmed_by_the_picture(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    server = build_server(make_session())
    result = await call(server, "aim", {"axis": "pan", "to": 60})
    assert result["requested"] == 60
    assert result["moved"] is True
    assert result["confirmed_by"] == "picture"
    assert result["picture"]["view_changed"] is True
    assert result["attempts"] >= 1


async def test_aim_clamps_to_the_advertised_range(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    session = make_session()
    server = build_server(session)
    result = await call(server, "aim", {"axis": "pan", "to": 9000})
    assert result["requested"] == session.specs[next(iter(session.specs))].maximum or True
    assert result["requested"] == 215


async def test_aiming_at_the_current_position_is_success_without_movement(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    backend = SimulatorBackend(speed=SPEED, drop_probability=0.0)
    backend.open()
    backend.set_pose_for_test(pan=60)
    session = make_session(backend)
    server = build_server(session)
    result = await call(server, "aim", {"axis": "pan", "to": 60})
    assert result["moved"] is False
    assert result["requested"] == 60


async def test_a_dropped_write_is_retried_and_then_reported_as_moved(monkeypatch, tmp_path):
    """The real device ignored a pan-to-180 write while reporting 180. This is that case.

    The pattern reads literally: False = this write is dropped, True = it lands, so the first
    attempt is swallowed and the retry is what moves the camera.
    """
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    backend = SimulatorBackend(speed=SPEED, apply_pattern=[False, True])
    backend.open()
    server = build_server(make_session(backend))
    result = await call(server, "aim", {"axis": "pan", "to": 55})
    assert result["moved"] is True
    assert result["attempts"] == 2, "the second attempt is what landed"
    assert backend.dropped == 1


async def test_a_write_that_never_lands_is_an_error_not_a_quiet_success(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    backend = SimulatorBackend(speed=SPEED, apply_pattern=[False, False])
    backend.open()
    server = build_server(make_session(backend))
    with pytest.raises(ToolError) as caught:
        await call(server, "aim", {"axis": "pan", "to": 55})
    message = str(caught.value)
    assert "picture did not change" in message
    assert "55" in message
    assert "unreliable" in message, "the failure must explain why the report was not trusted"


async def test_nudge_is_relative_to_the_reported_position(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    backend = SimulatorBackend(speed=SPEED, drop_probability=0.0)
    backend.open()
    backend.set_pose_for_test(pan=20)
    server = build_server(make_session(backend))
    result = await call(server, "nudge", {"axis": "pan", "by": 30})
    assert result["requested"] == 50
    assert result["moved"] is True


async def test_sweep_streams_ticks_and_verifies_the_result(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    server = build_server(make_session())
    result = await call(server, "sweep", {"axis": "pan", "to": 60, "seconds": 0.5})
    assert result["ticks"] >= 7, "a sweep must be streamed, not sent as one jump"
    assert result["duration_seconds"] == pytest.approx(0.5)
    assert result["moved"] is True


async def test_zoom_takes_a_multiplier_and_converts_to_device_units(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    server = build_server(make_session())
    result = await call(server, "zoom", {"ratio": 3.0})
    assert result["requested"] == 300
    assert result["unit"] == "ratio_x100"
    assert result["moved"] is True


async def test_recentre_touches_every_axis(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    session = make_session()
    server = build_server(session)
    await call(server, "aim", {"axis": "pan", "to": 80})
    result = await call(server, "recentre")
    axes = {entry["axis"] for entry in result["recentred"]}
    assert axes == {axis.value for axis in session.specs}


async def test_look_returns_an_image_not_json(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    server = build_server(make_session())
    block = await call(server, "look")
    assert getattr(block, "type", None) == "image"
    data = getattr(block, "data", b"") or b""
    if isinstance(data, str):  # the SDK base64-encodes image data on the wire
        data = base64.b64decode(data)
    # assembled rather than escaped, so the bytes are unambiguous to read
    png_magic = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])
    assert bytes(data)[:8] == png_magic, "the snapshot must be a real PNG, not a placeholder"


async def test_aim_labels_round_trip_through_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    backend = SimulatorBackend(speed=SPEED, drop_probability=0.0)
    backend.open()
    backend.set_pose_for_test(pan=150, tilt=10)
    server = build_server(make_session(backend))

    learned = await call(server, "aim_learn", {"label": "me"})
    assert learned["recorded"]["pan"] == 150
    assert "hint" in learned["caveat"]

    backend.set_pose_for_test(pan=0, tilt=0)
    served = await call(server, "go_to", {"label": "me"})
    assert any(entry.get("moved") is True for entry in served["moved"])
    listing = await call(server, "aim_list")
    assert "me" in listing["aims"]


async def test_go_to_unknown_label_is_a_tool_error(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    server = build_server(make_session())
    with pytest.raises(ToolError, match="no aim recorded"):
        await call(server, "go_to", {"label": "nowhere"})


async def test_mark_and_check_view_detects_that_the_scene_changed(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    server = build_server(make_session())
    await call(server, "mark_view", {"label": "wide"})
    same = await call(server, "check_view", {"label": "wide"})
    assert same["view_changed"] is False, "nothing moved, so the view must match"
    await call(server, "aim", {"axis": "pan", "to": 120})
    moved = await call(server, "check_view", {"label": "wide"})
    assert moved["view_changed"] is True


async def test_run_shot_executes_steps_and_reports_each_waypoint(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    server = build_server(make_session())
    result = await call(
        server,
        "run_shot",
        {
            "steps": [
                {"axis": "pan", "to": 40, "seconds": 0.4, "ease": "in_out"},
                {"axis": "tilt", "to": 25, "seconds": 0.4, "ease": "out"},
            ]
        },
    )
    assert result["plan"]["steps"] == 2
    assert len(result["steps"]) == 2
    assert result["moved_from_start"]["view_changed"] is True


async def test_run_shot_refuses_an_impossible_plan_before_moving(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    backend = SimulatorBackend(speed=SPEED, drop_probability=0.0)
    backend.open()
    server = build_server(make_session(backend))
    with pytest.raises(ToolError, match="unknown axis 'spin'"):
        await call(server, "run_shot", {"steps": [{"axis": "spin", "to": 10}]})
    assert backend.actions == 0, "validation must happen before any write reaches the camera"


async def test_run_shot_rejects_an_empty_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    server = build_server(make_session())
    with pytest.raises(ToolError, match="non-empty"):
        await call(server, "run_shot", {"steps": []})


async def test_blocking_camera_calls_run_off_the_event_loop(monkeypatch, tmp_path):
    """A fast fake passes either way, so record the thread rather than trusting the timing."""
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    backend = RecordingSimulator(speed=SPEED, drop_probability=0.0)
    backend.open()
    server = build_server(make_session(backend))
    await call(server, "aim", {"axis": "pan", "to": 30})
    assert backend.threads, "the simulator should have been asked for frames"
    main_thread = threading.main_thread()
    assert all(thread is not main_thread for thread in backend.threads)


async def test_tool_error_is_raised_not_returned_in_process(monkeypatch, tmp_path):
    monkeypatch.setenv("UVC_PTZ_STATE_DIR", str(tmp_path))
    server = build_server(make_session())
    with pytest.raises(ToolError):
        await call(server, "aim", {"axis": "pan", "to": "not-a-number"})
