"""MCP server for USB (UVC) pan/tilt/zoom cameras.

The server exposes a camera's *aim* as an outcome -- move, nudge, sweep, zoom, look -- rather
than its control surface, and it confirms every move against the picture instead of trusting
the device's own report.

Why picture-based confirmation: on the reference device (a DJI Osmo Pocket 4P in webcam mode)
the UVC control read-back was measured lying in both directions. A command to pan to 180 read
back "180" while the video proved the camera had not moved at all; another run read back 0 for
32 seconds while the frame demonstrably changed. An agent acting on that value would report a
move that never happened, so this server treats a device-reported value as a hint and the
rendered frame as the evidence.

Two backends ship:
  * DirectShow (Windows, `comtypes` + `pygrabber`) -- the real device path.
  * A simulator, calibrated from the measurements above, so the whole server can be exercised
    with no camera attached: it models the command latency, the per-axis settle time, the
    occasional silently-dropped write, and the read-back that echoes a requested value the
    hardware never applied.
"""

__all__ = ["__version__"]

# Keep this in step with pyproject.toml, server.json and the mcpb manifest: a host that asks
# reports this value, and nothing reads it during packaging, so it drifts silently.
__version__ = "0.1.0"
