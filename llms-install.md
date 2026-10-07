# Installing uvc-ptz-camera-mcp (instructions for an AI agent)

Follow these steps in order. The install is one command — there is nothing to build, clone,
compile, and **no account and no credential of any kind**. If a step asks you for a password,
you have followed the wrong instructions.

## 1. Check prerequisites

- Python 3.10 or newer. `uvx` (recommended) fetches its own interpreter if none is present.
- `ffmpeg` on `PATH` — needed to capture frames from a real camera. In simulator mode the server
  renders its own frames and needs no ffmpeg.
- For a real camera on Windows: `pip install "uvc-ptz-camera-mcp[dshow]"` (comtypes + pygrabber).

## 2. Add the server to the MCP client config

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

`uvx uvc-ptz-camera-mcp` installs from PyPI on first launch. Run `uvc-ptz-mcp --print-config` to
get this snippet with the right interpreter already filled in, and `uvc-ptz-mcp --list-devices`
to see the camera names available for `--device NAME`.

Useful flags: `--device NAME` (substring match, for a machine with several cameras),
`--backend dshow|simulator|auto`. Both also read from the environment (`UVC_PTZ_DEVICE`,
`UVC_PTZ_BACKEND`, `UVC_PTZ_STATE_DIR`), which is how the desktop bundle configures them; the flag
wins, and an empty value means "not set".

## 3. Verify the install

Call `camera_status`. A working answer names the device, lists each axis with its range, and says
whether the backend is real or simulated. Then call `look` once and confirm an image comes back.
Both tools exist only after a successful handshake, so seeing them at all proves the process
started.

## 4. Things that will bite you

- **A device-reported position is a hint, not a fact.** This class of camera reports angles it
  never moved to — measured on the reference camera, a command to pan 180 read back `180` while
  the picture proved nothing had moved. Every move tool therefore returns `moved` decided from
  the picture, and `device_reported` labelled as a hint. Never report a move as done because
  `device_reported` says so.
- **`moved: false` is not a failure.** It means the camera was already where you asked it to be.
  Do not retry.
- **A move tool raising an error means the picture did not change** after a bounded retry. That is
  a real failure: the write was likely dropped. Report it; do not loop.
- **`simulated: true` means no camera was touched.** Either none is attached, or the backend was
  not usable, and `camera_status` carries the reason. Say so in your answer rather than describing
  camera movement that did not happen.
- **Observation is user-initiated.** `look`, `mark_view` and `check_view` capture what the camera
  sees — which may be a room with people in it. Call them when the user asks for a look, not
  speculatively.
- **The real-device backend is Windows-only** (DirectShow). Elsewhere, pass `--backend simulator`
  explicitly so a simulated run is not mistaken for a silent fallback.
- **Latency is real**: roughly 0.4 s from write to motion, a pan takes about a second, a 4× zoom
  about 2.5 s. Tools wait for the axis to settle; a host timeout shorter than a few seconds will
  cut a move short.
- **A moving subject looks like a moving camera.** Frame comparison cannot tell them apart. For
  long shots, use `mark_view` before and `check_view` after.
