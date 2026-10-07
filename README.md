# uvc-ptz-camera-mcp

[![python](https://img.shields.io/pypi/pyversions/uvc-ptz-camera-mcp.svg)](https://pypi.org/project/uvc-ptz-camera-mcp/)
[![license](https://img.shields.io/pypi/l/uvc-ptz-camera-mcp.svg)](https://pypi.org/project/uvc-ptz-camera-mcp/)

<!-- mcp-name: io.github.AbhijatSaxena/uvc-ptz-camera-mcp -->

An [MCP](https://modelcontextprotocol.io) server for **USB (UVC) pan/tilt/zoom cameras**: aim
them, nudge them, sweep them smoothly, zoom, and look through them — with **every move
confirmed by comparing the picture before and after**.

No account, no cloud, no vendor SDK. One device, one USB cable.

```
camera_status       the camera, its axes and ranges, and whether it is real
aim                 point an axis at an absolute value, confirmed from the picture
nudge               move relative to where the device says it is
sweep               a smooth timed move, streamed at 15 Hz
zoom                set zoom as a multiplier (1x .. the camera's maximum)
recentre            every axis back to its default
look                one frame, returned as an image
aim_learn           remember "this direction is the desk"
aim_list            what has been recorded for this camera
go_to               point at a recorded direction
run_shot            a multi-step move, verified after every waypoint
mark_view           store the current picture under a label
check_view          has the picture changed since that label?
```

## Why this exists

This class of camera **reports positions it never moved to**. Measured on the reference device
(a DJI Osmo Pocket 4P in webcam mode, over its standard UVC controls):

- a command to pan to `180` read back **`180`** while the video proved the camera had not moved
  at all;
- a whole 32-second run read back `0` on every sample while the frame demonstrably changed;
- pan `120` read back `119`, pan `200` read back `199`.

An agent acting on those values reports moves that never happened. So this server treats every
device-reported value as a **hint** — returned, labelled, never trusted — and decides `moved`
by comparing frames.

The threshold is not a guess either. It was calibrated on labelled hardware frames: every
"same view" pair scored **≤ 0.104** (a 96-second quiet baseline, and two commands the hardware
ignored), every "view changed" pair scored **≥ 0.624** (a pan, a tilt, a 12× zoom, two aim
steps). The shipped threshold is the midpoint, **0.364** — a 6× separation — and the test suite
asserts it still separates them. The labelled pairs and the values the metric produced are kept in
the repository ([`tests/fixtures/`](tests/fixtures/)) so the number can be re-checked rather than
believed — as measurements, not pictures: no frame or video taken by a camera is committed here,
and a test fails if one appears. The measurements behind the calibration are in
[`docs/measurements.md`](docs/measurements.md).

## What keeps the agent honest

- **`moved` comes from the picture.** `device_reported` is returned beside it, labelled a hint.
- **A move that produced no change is an error, not a quiet success** — after a bounded retry,
  the tool fails naming what was requested and what was observed.
- **Already being at the target is success without movement**: `moved: false`, no error.
- **Blocking work never runs on the event loop** (COM calls, ffmpeg captures), pinned by a test
  that records which thread the camera was touched on.
- **Observation is explicit**: nothing takes a picture except a tool you called.
- **A refused write aborts a shot** rather than continuing to drive a camera that stopped
  listening.

## Install

```bash
uvx uvc-ptz-camera-mcp            # run without installing
pip install uvc-ptz-camera-mcp    # or install it
```

For a real camera on Windows you also need the DirectShow extras:

```bash
pip install "uvc-ptz-camera-mcp[dshow]"
```

Then point your host at it — `--print-config` emits the snippet with the right interpreter:

```bash
uvc-ptz-mcp --print-config
uvc-ptz-mcp --list-devices        # what to pass to --device
uvc-ptz-mcp --backend simulator   # try it with no camera attached
```

### Where to put it

Claude Desktop, Cursor, VS Code and friends all take the same shape — this is the whole
configuration, because there is nothing to authenticate:

```json
{
  "mcpServers": {
    "ptz-camera": {
      "command": "uvx",
      "args": ["uvc-ptz-camera-mcp"]
    }
  }
}
```

Add `"--device", "Osmo"` (any substring of the name `--list-devices` prints) when the machine has
more than one camera, and `"--backend", "simulator"` to work with none. Both can also be set in the
environment instead of the args — `UVC_PTZ_DEVICE`, `UVC_PTZ_BACKEND` and `UVC_PTZ_STATE_DIR` — which
is what the Claude Desktop bundle uses, and which survives a host restart without editing its config
again. A flag wins over the environment, and an empty value means "not set" (a host that leaves an
option blank writes `""`, not nothing). Claude Desktop also accepts the `.mcpb` bundle attached to
each [release](https://github.com/AbhijatSaxena/uvc-ptz-camera-mcp/releases) as a one-click install.

## Modes, and how you can tell which one you are in

| Backend | What it is |
|---|---|
| `auto` (default) | Prefer a real camera; fall back to the simulator, saying why |
| `dshow` | Windows DirectShow: the real device path |
| `simulator` | No hardware at all: a model calibrated from the measurements below |

Every result carries `"simulated": true` or `false`, and `camera_status` reports the reason when
the simulator is standing in. Nothing quietly pretends to be hardware: a caller must never
believe it is driving a camera when it is driving a model.

The simulator is **calibrated from the reference device**, not invented: ~0.4 s from write to
first motion, a pan completing in about a second, a 4× zoom in about 2.5 s, silently dropped
writes, and a read-back that echoes a request the hardware never applied. It renders frames by
cropping a wide panorama, so a simulated pan genuinely changes the pixels — a simulator with a
static picture would let verification pass vacuously.

## Honest limitations

- **The real-device path is Windows-only** (DirectShow). A Linux backend would use `v4l2`; the
  interface is in place, the implementation is not written.
- **No exposure, focus or white balance.** The reference camera exposes no Processing Unit at
  all — only pan/tilt/roll/zoom.
- **The vendor Extension Unit is unreachable on Windows**, so features that live there (on the
  reference camera, its built-in subject tracking) cannot be driven from software. Measured:
  `IKsControl` is refused on the device filter, `IKsTopologyInfo` lists three nodes and no
  vendor node, and `CreateNodeInstance` fails on all of them. Linux can reach it through
  `uvcvideo`'s `UVCIOC_CTRL_QUERY`; that is a separate backend.
- **A moving subject looks like a moving camera.** Frame comparison cannot tell the two apart;
  the metric is calibrated so that a static scene behaves, and `check_view` is there for
  judging a view rather than a move.
- **One unusable device must not hide the others.** A registered-but-unavailable virtual camera
  raised when its DirectShow moniker was bound, which aborted a listing that should have
  returned two working cameras. Enumeration now skips what it cannot load and says so, and the
  same rule applies inside the backend when it looks for the camera you named.
- **Measured latency sets the ceiling**: ~0.4 s from command to motion, so control loops run at
  a couple of hertz, not tens. That is the hardware's floor, not the software's.
- **The DirectShow path is tested but dormant.** The reference device was returned partway
  through development, so the hardware path is exercised up to its interface and typed
  correctly against it, while the simulator carries the test load. Treat the first run against
  a real camera as the true acceptance test.

## Development

```bash
pip install -e ".[dshow]" numpy pytest pytest-asyncio ruff
python -m pytest -q                     # 39 tests, three layers
python -m ruff check . && python -m ruff format --check .
```

The suite is layered deliberately: the tool surface in-process against the simulator (mapping,
validation, honest failure), pure unit tests for the compiler and the metric, and a real stdio
handshake as a subprocess — including one run with a device name that cannot exist, because a
server that dies at startup is invisible to every host and directory that lists it.

## Reference device

Built against a **DJI Osmo Pocket 4P** in webcam mode: `VID_2CA3` / `PID_0023`, exposing pan
−38…215°, tilt −33…105°, roll ±35°, zoom 100…1200 (1×–12×) as standard UVC camera controls.
Any UVC PTZ camera exposes the same surface; the ranges are read from the device at startup
rather than assumed, and nothing vendor-specific is hard-coded.

Not affiliated with, endorsed by, or supported by DJI.
