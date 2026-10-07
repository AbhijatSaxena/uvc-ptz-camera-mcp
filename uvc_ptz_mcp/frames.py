"""Frame input/output: a pure-Python PNG encoder, and ffmpeg only where it is unavoidable.

Encoding is deliberately *not* ffmpeg's job. `look` returns a PNG to the host, and routing that
through a subprocess made the tool fail with an opaque error on any machine without ffmpeg --
which is every CI runner, and any user who installed the package but not a video toolchain. A
greyscale PNG is a header plus a zlib stream, so it is written here in a dozen lines and works
everywhere the package does.

ffmpeg stays the only way frames are *captured* from a real camera (Windows DirectShow), because
nothing in the standard library can open one. A missing ffmpeg therefore fails with a message
that names the fix, and the simulator path needs it not at all -- which has to stay true, because
it is what the test suite and the container image rely on.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import numpy as np

DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 180

# Assembled rather than escaped, so the bytes are unambiguous to read and to copy.
PNG_MAGIC = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])

FFMPEG_MISSING = (
    "ffmpeg is required to capture frames from a real camera and it is not on PATH; "
    "install ffmpeg, or run with --backend simulator to work without hardware"
)


def require_ffmpeg() -> None:
    """Raise with the fix in the message, rather than a bare FileNotFoundError later."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(FFMPEG_MISSING)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    """One PNG chunk: length, tag, payload, CRC over tag+payload."""
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def png_bytes(frame: np.ndarray) -> bytes:
    """Encode a greyscale frame as PNG bytes, with no external process.

    The frames this package produces are 320x180 greyscale, so the whole file is a few tens of
    kilobytes and zlib does the only work that matters. Filter type 0 (None) per scanline keeps
    it simple; the verification metric never sees this encoding, only the metric's inputs do.
    """
    array = np.ascontiguousarray(frame.astype(np.uint8))
    height, width = array.shape[:2]
    raw = b"".join(b"\x00" + array[row].tobytes() for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit greyscale
    return (
        PNG_MAGIC
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 6))
        + _chunk(b"IEND", b"")
    )


def png_size(data: bytes) -> tuple[int, int]:
    """Read width and height back out of PNG bytes. Used by tests to check the encoder."""
    if not data.startswith(PNG_MAGIC):
        raise ValueError("not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _raw_grey(stream: bytes, width: int) -> np.ndarray:
    """Interpret raw 8-bit greyscale bytes as a 2-D array."""
    pixels = np.frombuffer(stream, dtype=np.uint8)
    if pixels.size == 0:
        raise ValueError("decoded no pixels")
    height = pixels.size // width
    return pixels[: height * width].reshape(height, width)


def decode_grey(path: str | Path, width: int = DEFAULT_WIDTH) -> np.ndarray:
    """Decode an image file to a greyscale array, scaled to `width`."""
    require_ffmpeg()
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
    return _raw_grey(result.stdout, width)


def capture_dshow(device_name: str, width: int = DEFAULT_WIDTH) -> np.ndarray:
    """Grab one frame from a DirectShow video device (Windows). Raises on failure."""
    require_ffmpeg()
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
    return _raw_grey(result.stdout, width)
