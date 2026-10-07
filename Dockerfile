# Container image for uvc-ptz-camera-mcp.
#
# Two honest notes about running this in a container.
#
# 1. A USB camera is not in the container unless you put it there: pass the device through
#    (`--device=/dev/video0` on Linux) or the server will fall back to its simulator and say so.
#    On Windows the DirectShow backend needs a Windows host, not a Linux container.
# 2. The simulator is the reason this image is worth building at all: a directory or registry
#    that evaluates a server by starting it and reading its tool list can do that here with no
#    hardware and no credentials, because the server always starts and always lists its tools.

FROM python:3.12-slim

# ffmpeg is how frames are captured and encoded: verification compares pictures, so the server
# needs a decoder even in simulator mode is not true -- but for real use it is required.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "uvc-ptz-camera-mcp>=0.1,<0.2"

# MCP over stdio, exactly as a host would run it. Recording and observing writes to the state
# directory, so give it a volume if you want calibration to survive the container.
VOLUME ["/root/.uvc-ptz-camera-mcp"]
ENTRYPOINT ["uvc-ptz-mcp"]
