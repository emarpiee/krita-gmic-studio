"""Fast channel/bit-depth shuffles on raw pixel buffers using strided slice assignment."""
import sys


def swap_rb(data, sample_bytes):
    """BGRA <-> RGBA for 4-channel buffers with 1 or 2 bytes per sample."""
    step = 4 * sample_bytes
    src = bytes(data)
    out = bytearray(len(src))
    for k in range(sample_bytes):
        out[k::step] = src[2 * sample_bytes + k::step]
        out[sample_bytes + k::step] = src[sample_bytes + k::step]
        out[2 * sample_bytes + k::step] = src[k::step]
        out[3 * sample_bytes + k::step] = src[3 * sample_bytes + k::step]
    return bytes(out)


def swap_bytes16(data):
    """Swap endianness of 16-bit samples."""
    src = bytes(data)
    out = bytearray(len(src))
    out[0::2] = src[1::2]
    out[1::2] = src[0::2]
    return bytes(out)


def u16_native_to_be(data):
    return swap_bytes16(data) if sys.byteorder == "little" else bytes(data)


def u16_be_to_native(data):
    return u16_native_to_be(data)


def u16_to_u8(data_native):
    """Keep the high byte of each native-endian 16-bit sample."""
    src = bytes(data_native)
    return src[1::2] if sys.byteorder == "little" else src[0::2]


def u8_to_u16(data):
    """Widen 8-bit samples to native-endian 16-bit (v * 257)."""
    src = bytes(data)
    out = bytearray(len(src) * 2)
    out[0::2] = src
    out[1::2] = src
    return bytes(out)


def sample_bytes(depth_name):
    if depth_name == "U8":
        return 1
    if depth_name == "U16":
        return 2
    raise ValueError("unsupported color depth %s" % depth_name)


def fit_rgba(data, w, h, tw, th, pixel_bytes):
    """Crop/pad packed pixels from w x h to tw x th, anchored top-left, padding with transparent black."""
    if (w, h) == (tw, th):
        return bytes(data)
    src = memoryview(data)
    keep = min(w, tw) * pixel_bytes
    pad = bytes((tw * pixel_bytes) - keep)
    rows = [bytes(src[y * w * pixel_bytes:y * w * pixel_bytes + keep]) + pad for y in range(min(h, th))]
    rows += [bytes(tw * pixel_bytes)] * (th - len(rows))
    return b"".join(rows)


def background_probes(data, w, h, pixel_bytes, alpha_offset, grid=7, max_points=6):
    """Pick transparent sample pixels [(x, y)] for background-fill detection, or [] when not applicable.

    Not applicable = the image is entirely transparent (a render filter on an empty layer is *meant* to
    fill it) or has fewer than 3 transparent sample points.
    """
    alpha = bytes(data)[alpha_offset::pixel_bytes]
    if not any(alpha):
        return []
    pts = []
    for gy in range(grid):
        for gx in range(grid):
            x, y = (2 * gx + 1) * w // (2 * grid), (2 * gy + 1) * h // (2 * grid)
            if alpha[y * w + x] == 0:
                pts.append((x, y))
    if len(pts) < 3:
        return []
    step = max(1, len(pts) // max_points)
    return pts[::step][:max_points]
