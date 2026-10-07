# USB control of the reference camera: measurements

What was measured, and what the server's behaviour is built on. The reference device is a **DJI
Osmo Pocket 4P in webcam mode** (`VID_2CA3` / `PID_0023`), measured on **2026-09-26** through its
standard UVC camera controls on Windows. It was returned to the store afterwards, so this file and
the fixture corpus beside it are the evidence: there are no more live measurements to take.

The calibration is kept in the repository at [`tests/fixtures/`](../tests/fixtures/) as
**measurements — 10 labelled pairs and the value the metric produced — with no frames**. The
captures from that session show a room somebody lives in, and two of them showed a person: no image
or video taken by a camera is in this repository, and a test fails if one appears. Where the
excluded captures pointed is recorded in text below, in the aim map, without the pictures.

**Summary: the control channel works and is fast** (sub-second command latency, moves complete in
1–3 s), and **nothing the device reports can be used as confirmation**. That second finding is why
every move tool in the server confirms itself from the picture.

Two earlier conclusions in this record were wrong, and they are kept because the wrong readings are
the instructive part:

- ~~"Motion could not be reproduced: not a usable control channel."~~ Too strong. With no commands
  the view holds to 0.036 (see *The quiet baseline*), while commanded runs show changes of
  0.46–1.21 and return trips that land on a bit-identical original. The gimbal moves.
- ~~"Commands land 12–17% of the time."~~ Wrong method. That came from commanding the *same value*
  repeatedly: once the camera reaches a target, identical commands are no-ops by definition. It
  measured "commands that changed the picture", not reliability.

## Device identity

| Field | Value |
|---|---|
| Friendly name | `OsmoPocket4P` |
| VID / PID | `0x2CA3` / `0x0023` |
| Interfaces | `MI_00` Camera (UVC), `MI_02` MEDIA (audio in) |
| DirectShow name | `OsmoPocket4P` (video), `Capture Input terminal (OsmoPocket4P)` (audio) |

PID `0x0023` matches the published Osmo Pocket 4 descriptor investigation
(`Rychillie/Pocket-Control-Lab`: `uvc,uac1`, high speed, H.265 1080p default, one vendor Extension
Unit at Unit 6), so the 4P sits on the same UVC surface as the Pocket 4.

## Advertised control surface

`IAMCameraControl` (Camera Terminal), all `manual`-only (`flags=2`):

| Control | min | max | step | default |
|---|---|---|---|---|
| Pan | −38 | 215 | 1 | 0 |
| Tilt | −33 | 105 | 1 | 0 |
| Roll | −35 | 35 | 1 | 0 |
| Zoom | 100 | 1200 | 1 | 100 |

- Degrees for the gimbal axes; zoom is ratio ×100 (100 = 1×, 1200 = 12×).
- Exposure, Iris and Focus are **not present**; `IAMVideoProcAmp` is **not exposed at all** — no
  brightness, contrast or white balance, as expected for a fixed-lens gimbal camera. The server
  therefore never claims to set any of them.
- Pan range is within 3° of the Pocket 3's published −35…215: the same control family.

## Nothing the device reports can be used as confirmation

`Set()` completes in 1.3–12 ms and never errors. What comes back is unusable:

- **Stale by one interaction.** Verbatim from the session's `test_pan_visual2.py`:

  ```
  [0] commanding Pan -> 0     set 0 (send 1) -> read-back 0     post-hold read-back: 0
  [1] commanding Pan -> 60    set 60 (send 1) -> read-back 0    post-hold read-back: 60
  [2] commanding Pan -> 0     set 0 (send 1) -> read-back 15    post-hold read-back: 0
  ```

  All three stills from that run show the SAME view (0.05–0.10): the read-back moved while the
  picture did not.
- **Off-by-one values**: Pan 120 read back `119`, Pan 200 read back `199`, Pan 90 once `0`.
- **A known case of it reading back a value it never applied**: commanded Pan 180, read back `180`,
  picture unchanged (see *Where the subject is*).
- **Sometimes it tracks reality**: `0 → 71 → 90` while the target was 90, which is what first
  revealed the easing.
- **Sometimes it tracks nothing**: `poll_profile.py` sampled `0` every 0.5 s for 32 s while the
  picture changed by 0.155.

Consequence, and the rule the server enforces: **confirmation comes from the picture, never from a
read-back.** The device's own value is returned as a labelled hint and used only to distinguish
"already there" from "the write did not land".

## The quiet baseline — the most useful result

12 stills over 96 s with **no commands at all**: maximum drift **0.036**.

So the capture path is clean and deterministic, the camera does not wander on its own, and the
0.46–1.21 changes in commanded runs were caused by the writes. The separate finding that opening
and closing the DirectShow stream around every measurement perturbs the camera (exposure
re-converges, giving an 18.8 noise floor) is why later measurements used one continuous recording
or stills taken while the camera was quiet.

## What a commanded move looks like

All with a static scene and a verified quiet baseline:

| Run | Command | Result |
|---|---|---|
| `observe_run.py` | Pan 0→90, 3 sends, 12 s hold | changed 1.210; commanding back to 0 returned to the **bit-identical** original (0.000) |
| `observe_run.py` | Tilt 40 / Tilt 0 | Tilt 40 changed nothing; Tilt 0 produced a view with the ceiling corner visible and the panel edge **skewed** |
| `observe_run.py` | Zoom 100→400 / 100 | 400 changed nothing; the later Zoom 100 changed the view |
| `repeat_test.py` | Pan 90 ×8 | one change, at repeat 2; read-backs `0 → 71 → 90` = easing over ~20 s |
| `poll_profile.py` | single Pan 0→20, 32 s polled | read-back stuck at 0; image moved only 0.155 |

A command is sometimes obeyed (large persistent change; return trips work), sometimes appears to do
nothing, and the effect can surface on a *later* command. The skewed panel edge matters — **a
digital crop cannot rotate the image**, so that view required real mechanical motion.

## Measured motion model

Measured with the session's `motion_timeline.py` — continuous 20 fps video, per-frame motion energy,
exposure-normalised — after the camera was turned to face a **textured** scene. That setup change is
crucial: pointed at a blank wall, a slow pan barely changes pixels, so an earlier detector reported
"no motion" while the camera was in fact turning.

| Move | Motion starts after write | Motion ends | Notes |
|---|---|---|---|
| Pan 0→60, one send | +0.54 s | +1.74 s | a single send worked; the "swallowed write" theory is dead |
| Pan 60→0, two sends | +0.36 s | +0.36 s | the detector caught one frame of it |
| Zoom 100→400 (4×), two sends | +0.42 s | ~+2.96 s | ~2.5 s of motion |

The read-back reached 90% of target at +1.2–1.4 s in these runs, roughly concurrent with the
physical move — which is why it sometimes looks like a progress meter. It is still not confirmation.

**These are the constants the server's simulator is built from** (`uvc_ptz_mcp/simulator.py`):
~0.4 s from write to first motion, a pan completing in about a second, a 4× zoom taking about 2.5 s.

Caveats, stated honestly:

- A **live person moving in frame contaminates the motion detector** — one zoom run reported 90% of
  frames "in motion" purely from the subject shifting. For clean numbers, point the camera at a
  static textured object with nobody moving.
- All latencies include host-side overhead (COM call, ffmpeg pipe); they are upper bounds.

## Methodology lessons (the reusable part)

1. **A value echoing back is not a move.** Verify against the picture.
2. **One baseline for many attempts is a false-positive generator.** Once anything moves, every
   later comparison is against a stale view.
3. **Opening and closing the capture stream per measurement perturbs the camera** (exposure
   re-converges). One continuous recording, or stills only while quiet.
4. **Repeating one command measures nothing about reliability** — after the first success the rest
   are no-ops. To measure reliability, alternate between two values.
5. **Establish the quiet baseline before believing any commanded change.**
6. **Look at the frames.** Numbers cannot separate "the optics moved" from "the exposure
   re-converged"; a person comparing two stills settles it in seconds.

## Where the subject is (pan aim map) and the motion demo

The pan axis is absolute and the useful direction is **not** around 0 on this unit. Measured by
commanding a ladder of pan values and looking at the resulting stills (`aim_room.py`):

| Pan | What the camera sees |
|---|---|
| 0 | wall, fabric panel at the right edge |
| 90 / 120 | intermediate views, neither wall nor subject |
| **150** | the subject, close |
| 180 | wall again — this write did **not** apply; the camera stayed where it was |
| **215** (max) | the subject plus the whole room |

So sweeping pan around 0 swings the camera off the subject. The server does not hard-code any of
this: the aim map is per-device and per-mounting, which is why `aim_learn` / `go_to` exist and why
the tool descriptions say a pan *value* means nothing until something has been recorded against it.

Of six pan targets sent with three writes each, five applied. The 180 row is another instance of
the read-back rule: the read-back said 180, the picture said "still at 0".

**Correction to the demo artefact.** Two recorded takes exist (27 s each: one sweeping around the
wall view, one centred on the subject with 66% of frame pairs showing visible change and a tracked
edge travelling 293 px). The schedule described them as covering a pan+tilt diagonal and a
figure-eight "pan and tilt in quadrature". That is not what happened: the tick loop wrote **one axis
per tick**, so those segments moved pan only, and the tilt and roll coverage claimed for them was
never exercised. The videos are therefore evidence for pan, zoom and tilt-separately, not for
simultaneous multi-axis motion. The server's compiler writes every moving axis of a tick for
exactly this reason, and `tests/test_motion.py` pins it.

The full-resolution videos (~11 MB and ~8 MB) are in the session directory rather than this
repository.

## OBS integration

The 4P is a plain UVC camera, so OBS takes it **directly** — no bridge, no relay, no virtual camera
in the path.

- obs-websocket was off in its config; enabling it must be done **while OBS is closed**, because OBS
  reads that file at launch.
- A source in a scene was created with `dshow_input`, and **the device id must come from OBS, not
  from the friendly name**: OBS accepts its own long moniker
  (`OsmoPocket4P:\\?\usb#vid_2ca3&pid_0023&mi_00#…#{65e8773d-…}\global`) obtained from its property
  list. A guessed id creates a source that looks correct and renders nothing.
- Verified by reading back an OBS-rendered frame, not by reading config. The first frame was a
  zoomed-in wall — the pipeline was fine, the camera had been left at 4× zoom by a preceding
  measurement.
- A second consumer cannot open the device while OBS holds it: recording via ffmpeg then produces a
  0 KB file. Either let OBS record, or close OBS first.

## Vendor Extension Unit: no route on Windows, a real one on Linux

The XU is where anything beyond the standard control set would live — most interestingly, whether
the camera's own on-device subject tracking can be started or retargeted from software. The standard
UVC set does not expose that.

Descriptor facts: Unit 6, GUID `41769EA2-04DE-E347-8B2B-F4341AFF003B`, `bNumControls = 2` but
`bmControls = 0x07`, so selectors 1–3 are candidates. The published investigation reports
**Tracking: UNKNOWN**; nothing public establishes the selector semantics.

Measured on Windows, read-only:

- `IKsControl` on the device filter: **QueryInterface refused**.
- `IKsTopologyInfo` on the filter: **present**, reporting **3 nodes** — capture, streaming and
  camera-terminal. **No DEV_SPECIFIC node, and none matching the XU GUID.**
- `CreateNodeInstance` for `IKsControl` on all three nodes: **fails**.

So the XU is in the USB descriptors but Windows/ksproxy does not surface it: there is nothing to
instantiate and nothing that accepts the property set. **No `SET_CUR` was ever issued**, by design —
the selectors are undocumented and a guessed payload can leave the unit needing a power cycle.

The route that does exist is Linux: `uvcvideo` exposes `UVCIOC_CTRL_QUERY` on `/dev/videoN`, which
sends UVC class requests for an arbitrary unit and selector — the documented equivalent of the
macOS investigation that produced the descriptor dump. A Raspberry Pi on the network would host it;
enumerate `GET_INFO` / `GET_LEN` / `GET_CUR` for unit 6, selectors 1–3, read-only, before anything
else. Windows cannot do this stage at all.

## What this calibrates in the server

| Measurement | Where it lives |
|---|---|
| Threshold 0.364 (highest "same" 0.1038, lowest "changed" 0.6242) | `verify.DEFAULT_THRESHOLD`, asserted against the corpus in `tests/test_verify.py` |
| Axis ranges and defaults | Read from the device at startup; never hard-coded |
| ~0.4 s latency, ~1 s pan, ~2.5 s 4× zoom, dropped writes, a lying read-back | `simulator.py`, so the honesty rules are testable with no hardware |
| The pan aim map | Deliberately *not* in the code: recorded per device with `aim_learn` |

## Still open

1. **Mechanical or digital PTZ?** The advertised pan/tilt/zoom may drive a crop rather than the
   motors. The rotation observed once argues against pure digital, but does not rule out a mix.
2. **A state gate.** The head may sit parked in webcam mode until something wakes it — the phone
   app, the joystick, or a gimbal mode. The Pocket 3 disables gimbal control entirely in portrait
   orientation, so state gating is known behaviour in this family.
3. **The Extension Unit**, via Linux, read-only first (above).
4. **The whole DirectShow path against real hardware.** This is the largest gap and the reason the
   README says the device path is written but dormant: the parts that could be measured without the
   device are measured, and the rest is untested until a camera is on a desk again.
