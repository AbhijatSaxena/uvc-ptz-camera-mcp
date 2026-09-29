"""Frame input/output without an image library: ffmpeg does the decoding and encoding.

Frames are greyscale `numpy` arrays because that is all the verification metric needs, and
because it keeps the dependency list to numpy plus whatever the backend already requires.
Encoding goes through ffmpeg as well, so a snapshot can be returned to the host as a real PNG
without pulling in Pillow.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 180


def decode_grey(path: str | Path, width: int = DEFAULT_WIDTH) -> np.ndarray:
    """Decode an image file to a greyscale array, scaled to `width`."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            f"scale={width}:-2",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        capture_output=True,
        timeout=60,
        check=True,
    )
    pixels = np.frombuffer(result.stdout, dtype=np.uint8)
    if pixels.size == 0:
        raise ValueError(f"decoded no pixels from {path}")
    height = pixels.size // width
    return pixels[: height * width].reshape(height, width)


def capture_dshow(device_name: str, width: int = DEFAULT_WIDTH) -> np.ndarray:
    """Grab one frame from a DirectShow video device (Windows). Raises on failure."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "dshow",
            "-rtbufsize",
            "200M",
            "-i",
            f"video={device_name}",
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        capture_output=True,
        timeout=90,
        check=False,  # the return code is inspected below, with the device named in the error
    )
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode(errors="replace").strip()[:200] or "no output"
        raise RuntimeError(f"could not capture a frame from {device_name!r}: {detail}")
    pixels = np.frombuffer(result.stdout, dtype=np.uint8)
    height = pixels.size // width
    return pixels[: height * width].reshape(height, width)


def png_bytes(frame: np.ndarray) -> bytes:
    """Encode a greyscale frame as PNG bytes, for returning to a host as image content."""
    array = np.ascontiguousarray(frame.astype(np.uint8))
    height, width = array.shape[:2]
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-s",
            f"{width}x{height}",
            "-i",
            "-",
            "-f",
            "image2",
            "-vcodec",
            "png",
            "-",
        ],
        input=array.tobytes(),
        capture_output=True,
        timeout=60,
        check=True,
    )
    return result.stdout
