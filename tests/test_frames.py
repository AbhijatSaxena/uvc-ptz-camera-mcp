"""Layer 1d: frame I/O, including the paths that must work with no video toolchain.

Everything here runs on a machine with no ffmpeg, on purpose. The failure CI caught was exactly
this: PNG encoding went through ffmpeg, so `look` returned an opaque error on any host without it
— and every CI runner is such a host. The rule this file pins is that *encoding* is the package's
own job and *capturing* is the only thing that needs an external tool.
"""

from __future__ import annotations

import struct
import zlib

import numpy as np
import pytest

from uvc_ptz_mcp import frames


def chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Split PNG bytes into (tag, payload) pairs, validating each length and CRC."""
    assert data.startswith(frames.PNG_MAGIC)
    out = []
    offset = len(frames.PNG_MAGIC)
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        tag = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF, f"bad CRC in {tag!r}"
        out.append((tag, payload))
        offset += 12 + length
    return out


def test_png_carries_the_pixels_it_was_given():
    """Not just "it starts with a PNG header": the inflated data must be the frame."""
    rows, columns = 12, 16
    frame = np.arange(rows * columns, dtype=np.uint8).reshape(rows, columns)
    data = frames.png_bytes(frame)

    tags = [tag for tag, _ in chunks(data)]
    assert tags == [b"IHDR", b"IDAT", b"IEND"]
    assert frames.png_size(data) == (columns, rows)

    header, _ = chunks(data)[0]
    assert header  # keep the unpack below honest about which chunk it reads
    mode = [payload for tag, payload in chunks(data) if tag == b"IHDR"][0]
    width, height, depth, colour = struct.unpack(">IIBB", mode[:10])
    assert (width, height, depth, colour) == (columns, rows, 8, 0), "8-bit greyscale"

    compressed = [payload for tag, payload in chunks(data) if tag == b"IDAT"][0]
    raw = zlib.decompress(compressed)
    assert len(raw) == rows * (columns + 1), "one filter byte per scanline"
    rebuilt = np.frombuffer(
        b"".join(raw[row * (columns + 1) + 1 : (row + 1) * (columns + 1)] for row in range(rows)),
        dtype=np.uint8,
    ).reshape(rows, columns)
    assert (rebuilt == frame).all()


def test_encoding_works_when_ffmpeg_is_absent(monkeypatch):
    """The CI condition: no ffmpeg anywhere on PATH."""
    monkeypatch.setattr(frames.shutil, "which", lambda _name: None)
    data = frames.png_bytes(np.full((10, 10), 128, dtype=np.uint8))
    assert data.startswith(frames.PNG_MAGIC)
    assert frames.png_size(data) == (10, 10)


def test_capture_without_ffmpeg_names_the_fix(monkeypatch):
    """A missing tool is an expected failure, and the message has to say what to do about it."""
    monkeypatch.setattr(frames.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError) as raised:
        frames.capture_dshow("Some Camera")
    message = str(raised.value)
    assert "ffmpeg" in message
    assert "--backend simulator" in message, "the message must offer the way out"


def test_decode_without_ffmpeg_names_the_fix(monkeypatch, tmp_path):
    monkeypatch.setattr(frames.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        frames.decode_grey(tmp_path / "nothing.png")
