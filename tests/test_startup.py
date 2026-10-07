"""Layer 2 and 3: a real stdio handshake, a wire-level failure, and the no-camera start.

In-process tests cannot catch a server that will not start, and a fast fake proves nothing about
the wire. So these drive the real entry point as a subprocess and speak JSON-RPC to it.

The third check is the one directories and health checks depend on: started with a device name
that cannot exist, the server must still answer `tools/list` and explain the situation, because a
host lists tools before it knows what hardware is attached. A process that dies at startup is
invisible to every listing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

try:  # the SDK owns the protocol version; never hard-code a guess
    from mcp.types import LATEST_PROTOCOL_VERSION as PROTOCOL_VERSION
except Exception:  # noqa: BLE001 - older layouts
    PROTOCOL_VERSION = "2025-06-18"

EXPECTED_TOOLS = {
    "camera_status",
    "aim",
    "nudge",
    "sweep",
    "zoom",
    "recentre",
    "look",
    "aim_learn",
    "aim_list",
    "go_to",
    "plan_shot",
    "run_shot",
    "mark_view",
    "check_view",
    "track_start",
    "track_status",
    "track_stop",
}


def initialize_messages() -> list[dict]:
    """Build the three messages a host sends before it can list tools."""
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "startup-test", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]


class Wire:
    """A running server subprocess, with the responses it has answered so far."""

    def __init__(self, args: list[str], timeout: float = 60.0) -> None:
        """Start the server and begin draining its output on a thread."""
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvc_ptz_mcp", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.lines: list[str] = []
        self._answered: set[int] = set()
        self._event = threading.Event()
        self._timeout = timeout
        self._pump = threading.Thread(target=self._read_loop, daemon=True)
        self._pump.start()

    def _read_loop(self) -> None:
        """Read every line the server writes until it closes stdout."""
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.append(line)
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                message = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(message.get("id"), int):
                self._answered.add(message["id"])
                if message["id"] >= 2:
                    self._event.set()

    def send(self, message: dict) -> None:
        """Write one message and flush, keeping stdin open for what comes next."""
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def wait_for(self, message_id: int = 2) -> dict:
        """Block until the response with this id arrives. Stdin stays open meanwhile."""
        deadline = threading.Event()
        for _ in range(int(self._timeout * 10)):
            for line in list(self.lines):
                stripped = line.strip()
                if not stripped.startswith("{"):
                    continue
                try:
                    message = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == message_id:
                    return message
            deadline.wait(0.1)
        raise AssertionError(
            f"no response with id {message_id} within {self._timeout}s; "
            f"answered ids: {sorted(self._answered)}; output: {self.lines[-10:]}"
        )

    def close(self) -> None:
        """Close stdin, then terminate, then kill: never leave a stray server behind."""
        try:
            if self.process.stdin:
                self.process.stdin.close()
            self.process.terminate()
            self.process.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.process.kill()

    def stderr(self) -> str:
        """Whatever the server logged (stdout is the protocol channel, so this is stderr)."""
        try:
            assert self.process.stderr is not None
            return self.process.stderr.read()
        except Exception:  # noqa: BLE001
            return ""


def test_stdio_handshake_lists_every_tool():
    wire = Wire(["--backend", "simulator"])
    try:
        for message in initialize_messages():
            wire.send(message)
        response = wire.wait_for(2)
        names = {tool["name"] for tool in response["result"]["tools"]}
        assert EXPECTED_TOOLS <= names, f"missing tools: {EXPECTED_TOOLS - names}"
        for tool in response["result"]["tools"]:
            assert tool.get("description"), f"{tool['name']} has no description for the agent"
    finally:
        wire.close()


def test_wire_level_bad_arguments_come_back_as_an_error():
    wire = Wire(["--backend", "simulator"])
    try:
        for message in initialize_messages():
            wire.send(message)
        wire.wait_for(2)
        wire.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "aim", "arguments": {"axis": "pan", "to": "not-a-number"}},
            }
        )
        response = wire.wait_for(3)
        assert "error" in response or response["result"].get("isError") is True, (
            "a failed call must reach the host as a failure"
        )
    finally:
        wire.close()


def test_server_starts_and_lists_tools_with_no_camera_present():
    """The listing must survive a missing camera: hosts and directories list before they probe."""
    wire = Wire(["--backend", "dshow", "--device", "definitely-not-a-real-camera-xyz"])
    try:
        for message in initialize_messages():
            wire.send(message)
        response = wire.wait_for(2)
        names = {tool["name"] for tool in response["result"]["tools"]}
        assert EXPECTED_TOOLS <= names

        wire.send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "camera_status", "arguments": {}},
            }
        )
        status = wire.wait_for(4)
        payload = json.loads(status["result"]["content"][0]["text"])
        assert payload["backend"]["simulated"] is True, "the fallback must be labelled"
        assert payload["start_error"], "the reason must be reported, not swallowed"
    finally:
        wire.close()
