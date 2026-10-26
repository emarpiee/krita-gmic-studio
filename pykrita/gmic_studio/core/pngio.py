"""Minimal PNG writer/header reader (8/16-bit gray or RGBA). Pure Python, no Pillow/numpy."""
import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
GRAY, RGBA = 0, 6
_CHANNELS = {GRAY: 1, RGBA: 4}


def _chunk(tag, payload):
    body = tag + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def encode(width, height, data, color_type=RGBA, bit_depth=8):
    """`data` is tightly packed rows; 16-bit samples must be big-endian."""
    if color_type not in _CHANNELS or bit_depth not in (8, 16):
        raise ValueError("unsupported PNG format")
    stride = width * _CHANNELS[color_type] * (bit_depth // 8)
    if len(data) != stride * height:
        raise ValueError("pixel buffer is %d bytes, expected %d" % (len(data), stride * height))
    view = memoryview(data)
    rows = [b"\x00" + bytes(view[y * stride:(y + 1) * stride]) for y in range(height)]
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    return (_SIGNATURE + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(b"".join(rows), 1)) + _chunk(b"IEND", b""))


def write(path, width, height, data, color_type=RGBA, bit_depth=8):
    with open(path, "wb") as fh:
        fh.write(encode(width, height, data, color_type, bit_depth))


def read_header(path):
    """Return (width, height, bit_depth, color_type) of a PNG file."""
    with open(path, "rb") as fh:
        head = fh.read(29)
    if len(head) < 29 or head[:8] != _SIGNATURE or head[12:16] != b"IHDR":
        raise ValueError("not a PNG file: %s" % path)
    width, height, depth, ctype = struct.unpack(">IIBB", head[16:26])
    return width, height, depth, ctype
