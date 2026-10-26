"""Reads the filter catalogue that G'MIC-Qt (Krita's built-in G'MIC) caches in `gmic_qt_filters.dat`.

The file is a Qt QDataStream (big-endian). Per filter, after a 32-char hash string:
    3 x title string, 3 x (1-byte count + strings) paths, command, preview command, 1 byte,
    parameter text, double, bool, bool
where a string is a 4-byte length plus UTF-8. The first record has a 20-byte binary hash.
Parsing is defensive: on any mismatch it resynchronises on the next hash, so a format change
degrades to "fewer filters" and never raises.
"""
import os
import re
import struct

from . import filters, paths

CACHE_NAME = "gmic_qt_filters.dat"
_HASH_RE = re.compile(rb"\x00\x00\x00\x20([0-9a-f]{32})(?=\x00\x00\x00)")
_MAX_STR = 1 << 20


class _Bad(Exception):
    pass


class _Reader:
    def __init__(self, data, pos):
        self.d, self.p = data, pos

    def string(self):
        if self.p + 4 > len(self.d):
            raise _Bad()
        (n,) = struct.unpack_from(">I", self.d, self.p)
        if n > _MAX_STR or self.p + 4 + n > len(self.d):
            raise _Bad()
        s = self.d[self.p + 4:self.p + 4 + n]
        self.p += 4 + n
        return s.decode("utf-8", "replace")

    def strings(self):
        if self.p >= len(self.d):
            raise _Bad()
        count = self.d[self.p]
        self.p += 1
        return [self.string() for _ in range(count)]


def _record(data, pos):
    """Parse one record whose hash string starts at `pos`; return (FilterDef|None, end)."""
    r = _Reader(data, pos)
    r.string()                                   # hash
    if data[r.p:r.p + 4] == b"\x00\x00\x00\x00":
        r.p += 1                                 # one flag byte follows the string hash
    title, plain, _name = r.string(), r.string(), r.string()
    _html_path = r.strings()
    path = r.strings()
    r.strings()
    command = r.string()
    preview = r.string()                         # preview command (may be the same, or a placeholder)
    r.p += 1                                     # one-byte setting between command and parameters
    params = r.string()
    r.p += 8 + 1 + 1                             # preview factor, two flags
    return title, plain, path, command, preview, params, r.p


def parse(data):
    """Return a list of FilterDef parsed from the raw bytes of a gmic_qt_filters.dat."""
    starts = []
    if len(data) > 12 and data[8:12] == b"\x00\x00\x00\x14":
        starts.append(8)                          # first record, binary SHA-1
    starts += [m.start() for m in _HASH_RE.finditer(data)]
    out, seen = [], set()
    for pos in starts:
        if pos in seen:
            continue
        try:
            title, plain, path, command, preview, params, end = _record(data, pos)
        except (_Bad, struct.error, IndexError):
            continue
        seen.add(pos)
        if not filters.COMMAND_RE.match(command) or command.startswith("_none"):
            continue                              # `_none_` marks info-only entries ("About")
        name = (plain or title).strip()
        fdef = filters.FilterDef(path=tuple(p.strip() for p in path if p.strip()), name=name, command=command,
                                 preview_command=filters.useful_preview(command, preview))
        fdef.params = filters.parse_param_block(params)
        fdef.preview_factor = struct.unpack(">d", data[end - 10:end - 2])[0]
        out.append(fdef)
    return out


def default_cache_path():
    for folder in paths.gmic_config_dirs():
        cand = os.path.join(folder, CACHE_NAME)
        if os.path.isfile(cand):
            return cand
    return None


def load(path=None):
    path = path or default_cache_path()
    if not path:
        return [], None
    try:
        with open(path, "rb") as fh:
            return parse(fh.read()), path
    except OSError:
        return [], None
