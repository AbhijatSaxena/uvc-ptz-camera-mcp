"""Start the server over stdio, do a real handshake, and report what it answers.

Used by CI after installing the built wheel into a clean environment, which is the only place a
missing runtime dependency shows up: the development virtualenv already has every package the
source tree imports, so `pytest` cannot see the difference between "declared" and "installed".

Reads with a watchdog and a hard deadline: a server that dies, or one waiting for something that
will never arrive, must fail this check rather than hang it.

Usage:  python scripts/handshake.py [path/to/python]
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from typing import Any

DEADLINE_SECONDS = 45.0
INITIALIZE_ID = 1
TOOLS_LIST_ID = 2
FALLBACK_PROTOCOL = "2025-11-25"
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
    "run_shot",
    "plan_shot",
    "mark_view",
    "check_view",
}


def protocol_version(python: str) -> str:
    """Ask the interpreter that will serve which protocol revision to propose.

    Asked of the target environment rather than this script's own: CI runs this file with an
    interpreter that has no MCP SDK installed at all, and the two are not always the same version.
    """
    try:
        result = subprocess.run(
            [python, "-c", "from mcp.types import LATEST_PROTOCOL_VERSION as v; print(v)"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        candidate = result.stdout.strip()
        if candidate:
            return candidate
    except Exception:  # noqa: BLE001 - any failure means "use the fallback"
        pass
    return FALLBACK_PROTOCOL


def spawn(python: str) -> tuple[subprocess.Popen, list[dict[str, Any]], threading.Lock]:
    """Start the server and begin collecting its replies."""
    process = subprocess.Popen(
        [python, "-m", "uvc_ptz_mcp", "--backend", "simulator"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    received: list[dict[str, Any]] = []
    lock = threading.Lock()

    def pump() -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            stripped = raw.strip()
            if not stripped.startswith("{"):
                continue
            try:
                message = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            with lock:
                received.append(message)

    threading.Thread(target=pump, daemon=True).start()
    return process, received, lock


def wait_for(received: list[dict[str, Any]], lock: threading.Lock, ids: set[int]) -> bool:
    """Wait until every id in `ids` has been answered, or the deadline passes."""
    deadline = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < deadline:
        with lock:
            if ids <= {message.get("id") for message in received}:
                return True
        time.sleep(0.05)
    return False


def answer(received: list[dict[str, Any]], lock: threading.Lock, ident: int) -> dict[str, Any]:
    """Return the reply with this id."""
    with lock:
        return next(message for message in received if message.get("id") == ident)


def main(argv: list[str]) -> int:
    """Run the handshake, printing what came back and failing loudly on silence."""
    python = argv[1] if len(argv) > 1 else sys.executable
    process, received, lock = spawn(python)

    requests = [
        {
            "jsonrpc": "2.0",
            "id": INITIALIZE_ID,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version(python),
                "capabilities": {},
                "clientInfo": {"name": "handshake", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": TOOLS_LIST_ID, "method": "tools/list", "params": {}},
    ]
    assert process.stdin is not None
    for request in requests:
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()

    ok = wait_for(received, lock, {INITIALIZE_ID, TOOLS_LIST_ID})

    # Close stdin only after the answers arrive: a closed pipe makes a stdio server exit before
    # it replies, which is what makes `tools/list` mysteriously go missing.
    process.stdin.close()
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        process.kill()

    if not ok:
        stderr = (process.stderr.read() if process.stderr else "") or ""
        print("FAILED: the server did not answer the handshake", file=sys.stderr)
        print(f"  answered: {[m.get('id') for m in received]}", file=sys.stderr)
        print(f"  stderr: {stderr.strip()[-800:]}", file=sys.stderr)
        return 1

    init = answer(received, lock, INITIALIZE_ID)["result"]
    tools = answer(received, lock, TOOLS_LIST_ID)["result"]["tools"]
    names = {tool["name"] for tool in tools}
    print(f"  serverInfo: {init['serverInfo']['name']} {init['serverInfo']['version']}")
    print(f"  protocol:   {init['protocolVersion']}")
    print(f"  tools:      {len(names)}")
    missing = EXPECTED_TOOLS - names
    if missing:
        print(f"FAILED: missing tools: {sorted(missing)}", file=sys.stderr)
        return 1
    print("  handshake ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
