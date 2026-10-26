"""Parser for G'MIC filter definitions (`#@gui` lines), parameter model and search."""
import glob
import html
import os
import re
from dataclasses import dataclass, field

from . import paths

BUILTIN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "builtin_filters.gmic")
COMMAND_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GUI_RE = re.compile(r"^#@gui(?:_en)?[ \t]+(.*)$")
# `=type(args)`; type may carry prefixes (`_` = hidden-ish, `~` = keep on reset); notes may use {} instead of ()
_PARAM_RE = re.compile(r"^(.*?)\s*=\s*[_~]*(\w+)\s*[({](.*)[)}](?:_(\d)([+*-]?))?\s*$", re.DOTALL)  # optional `_0`, `_1+` ... suffix
_TAG_RE = re.compile(r"<[^>]+>")
_UPDATE_RE = re.compile(r"update(\d+)\.gmic$")

NON_VALUE_KINDS = ("note", "separator", "link")


def _num(text, default=0.0):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def split_args(text):
    """Split on commas outside quotes/parentheses. Returns [(token, was_quoted)]."""
    out, cur, quoted, in_q, depth, i = [], [], False, False, 0, 0
    while i < len(text):
        ch = text[i]
        if in_q:
            if ch == "\\" and i + 1 < len(text):
                if text[i + 1] in '"\\':
                    cur.append(text[i + 1])           # \" and \\ are real escapes
                else:
                    cur.append(text[i:i + 2])         # keep \n etc. exactly as written
                i += 2
                continue
            if ch == '"':
                in_q = False
            else:
                cur.append(ch)
        elif ch == '"':
            in_q, quoted = True, True
        elif ch in "([":
            depth += 1
            cur.append(ch)
        elif ch in ")]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append(("".join(cur).strip() if not quoted else "".join(cur), quoted))
            cur, quoted = [], False
        else:
            cur.append(ch)
        i += 1
    tail = "".join(cur).strip() if not quoted else "".join(cur)
    if tail or quoted or out:
        out.append((tail, quoted))
    return out


def quote(text):
    """Quote a string for a G'MIC command so it can never close its own quotes.

    Real newlines become the two characters `\\n` (how G'MIC writes multi-line text, same as the
    definitions' own defaults); a literal `\\n` is kept; every other backslash and every quote is escaped.
    """
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\n":
            out.append("\\n")
        elif ch == "\\":
            if text[i + 1:i + 2] == "n":
                out.append("\\n")
                i += 1
            else:
                out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch >= " " or ch == "\t":
            out.append(ch)
        i += 1
    return '"' + "".join(out) + '"'


@dataclass
class Param:
    label: str
    kind: str
    default: object = None
    minimum: float = 0.0
    maximum: float = 100.0
    choices: list = field(default_factory=list)
    multiline: bool = False
    extra: str = ""
    color: list = None            # point parameters: the handle's color [r, g, b]
    propagate: str = ""           # `+`, `-` or `*`: the visibility also covers the notes after / before / around it
    visibility: int = 2           # 0 = hidden (still sent), 1 = shown grayed-out, 2 = shown (G'MIC's `_0` / `_1` / `_2` suffix)

    @property
    def has_value(self):
        return self.kind not in NON_VALUE_KINDS

    def coerce(self, value):
        """Clamp/convert an arbitrary (possibly stale preset) value to something valid."""
        if value is None:
            return self.default
        try:
            if self.kind == "int":
                return int(max(self.minimum, min(self.maximum, round(float(value)))))
            if self.kind == "float":
                return max(self.minimum, min(self.maximum, float(value)))
            if self.kind == "bool":
                return bool(value)
            if self.kind == "choice":
                idx = int(value)
                return idx if 0 <= idx < max(1, len(self.choices)) else self.default
            if self.kind == "color":
                vals = [int(max(0, min(255, round(float(v))))) for v in value]
                return vals if len(vals) in (3, 4) else self.default
            if self.kind == "point":
                x, y = value
                return [float(x), float(y)]
            if self.kind == "button":
                return 1 if value in (1, True, "1") else 0
        except (TypeError, ValueError):
            return self.default
        return str(value)

    def serialize(self, value):
        """Command-line fragment for this parameter, or None if it takes no slot."""
        if not self.has_value:
            return None
        value = self.coerce(value)
        k = self.kind
        if k == "int" or k == "choice":
            return str(int(value))
        if k == "float":
            return "%.10g" % value
        if k == "bool":
            return "1" if value else "0"
        if k == "color":
            return ",".join(str(int(v)) for v in value)
        if k == "point":
            return "%.10g,%.10g" % (value[0], value[1])
        if k in ("text", "file", "folder"):
            return quote(value)
        if k == "button":
            return str(value)
        return str(self.default if self.default is not None else 0)  # value / hidden / unknown


def _unesc(text):
    """G'MIC escapes ( ) , " etc. as numeric entities inside parameter definitions."""
    return html.unescape(text)


def _make_param(label, kind, args):
    label = _unesc(_TAG_RE.sub("", label).strip())
    toks = [t for t, _ in args]
    quoted = [q for _, q in args]
    p = Param(label=label, kind=kind)
    if kind == "int":
        p.default = int(_num(toks[0]) if toks else 0)
        p.minimum = _num(toks[1], 0) if len(toks) > 1 else 0
        p.maximum = _num(toks[2], 100) if len(toks) > 2 else 100
        p.default = int(p.default)
        p.minimum, p.maximum = min(p.minimum, p.default), max(p.maximum, p.default)    # the stated default always fits
    elif kind == "float":
        p.default = _num(toks[0]) if toks else 0.0
        p.minimum = _num(toks[1], 0) if len(toks) > 1 else 0.0
        p.maximum = _num(toks[2], 100) if len(toks) > 2 else 100.0
        p.minimum, p.maximum = min(p.minimum, p.default), max(p.maximum, p.default)
    elif kind == "bool":
        p.default = bool(toks) and toks[0].strip().lower() in ("1", "true")
    elif kind == "choice":
        items = list(args)
        idx = 0
        if items and not items[0][1] and re.fullmatch(r"-?\d+", items[0][0] or ""):
            idx = int(items.pop(0)[0])
        p.choices = [_unesc(_TAG_RE.sub("", t)) for t, _ in items]
        p.default = idx if 0 <= idx < max(1, len(p.choices)) else 0
    elif kind == "text":
        items = list(args)
        if len(items) >= 2 and not items[0][1] and items[0][0] in ("0", "1"):
            p.multiline = items.pop(0)[0] == "1"
        p.default = _unesc(items[0][0]) if items else ""
    elif kind == "color":
        hexcol = re.fullmatch(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{8})", toks[0].strip()) if toks else None
        if hexcol:
            digits = hexcol.group(1)
            nums = [int(digits[i:i + 2], 16) for i in range(0, len(digits), 2)]
        else:
            nums = [int(_num(t)) for t, q in args if not q][:4]
        p.default = (nums + [0, 0, 0])[:max(3, len(nums))] if nums else [0, 0, 0]
    elif kind == "point":
        p.default = [_num(toks[0], 50.0) if toks else 50.0, _num(toks[1], 50.0) if len(toks) > 1 else 50.0]
        if len(toks) >= 7:                                  # point(x, y, removable, burst, r, g, b, a, radius)
            p.color = [max(0, min(255, int(_num(t)))) for t in toks[4:7]]
    elif kind in ("file", "folder"):
        p.default = toks[0] if toks else ""
    elif kind in ("note", "link"):
        p.default = toks[0] if toks else ""
        p.extra = toks[1] if len(toks) > 1 else ""
    elif kind in ("value", "hidden"):
        p.default = ",".join(toks) if toks else None   # passes all of its numbers, e.g. value(50,50)
    elif kind == "button":
        p.default = 0                                       # 1 only for the run started by pressing it
    elif kind == "separator":
        p.default = toks[0] if toks else None
    else:
        p.kind, p.default = "hidden", (toks[0] if toks else None)
    return p


@dataclass
class FilterDef:
    path: tuple
    name: str
    command: str
    params: list = field(default_factory=list)
    # G'MIC's per-filter preview zoom: 0 = needs a 1:1 crop, 1 = made for a whole-image thumbnail, -1 = either
    preview_factor: float = 0.0
    # The filter's own preview command (takes the same arguments). It handles preview-only options such as
    # "Preview Brush", "Preview Split" or "Preview Type". Empty = just use the filter's command.
    preview_command: str = ""

    @property
    def id(self):
        return " > ".join(self.path + (self.name,))

    def defaults(self):
        return {str(i): p.default for i, p in enumerate(self.params) if p.has_value}

    def command_line(self, values=None):
        """Return the G'MIC command (with arguments) for the given {index: value} mapping."""
        if not COMMAND_RE.match(self.command):
            raise ValueError("illegal command name: %r" % self.command)
        values = values or {}
        frags = []
        for i, p in enumerate(self.params):
            frag = p.serialize(values.get(str(i), p.default))
            if frag is not None:
                frags.append(frag)
        return self.command, frags


_TYPE_ALIASES = {"filein": "file", "fileout": "file"}


_NO_PREVIEW = ("gui_no_preview",)


def useful_preview(command, preview):
    """The preview command worth running, or '' when it is missing, the same, a placeholder or 'no preview'."""
    preview = (preview or "").strip().lstrip("-")
    if not COMMAND_RE.match(preview) or preview == command or preview.startswith("_none") or preview in _NO_PREVIEW:
        return ""
    return preview


def _parse_preview_spec(text):
    """`fx_preview(1)*` -> ("fx_preview", 1.0); anything unreadable -> ("", None)."""
    m = re.match(r"\s*-?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*(-?[0-9.]+)\s*\))?", text or "")
    if not m:
        return "", None
    return m.group(1), (float(m.group(2)) if m.group(2) is not None else None)


def parse_param_line(line):
    """Parse `label = type(args)`; returns a Param or None."""
    pm = _PARAM_RE.match(line.strip())
    if not pm:
        return None
    kind = pm.group(2).lower()
    param = _make_param(pm.group(1), _TYPE_ALIASES.get(kind, kind), split_args(pm.group(3)))
    if pm.group(4):
        param.visibility = int(pm.group(4))
        param.propagate = pm.group(5)
    inner = pm.group(3).strip()
    if param.kind == "note" and len(inner) >= 2 and inner[0] == inner[-1] == '"':
        param.default = inner[1:-1]      # notes carry raw HTML with unescaped quotes (href="...")
    return param


# A line that clearly starts a new parameter: `label=type(` / `label=type{` with no quote in the label.
_ENTRY_START = re.compile(r'^[^"\n=]*=\s*[_~]*\w+\s*[({]')


def split_param_block(text):
    """Split a multi-line parameter block into entries.

    Normally one entry per line; only multi-line notes continue onto following lines. Quote balance is not
    trusted (some definitions have stray quotes), so a line that looks like a new parameter always starts one.
    """
    entries = []
    for line in text.split("\n"):
        if not entries or _ENTRY_START.match(line):
            entries.append(line)
        else:
            entries[-1] += "\n" + line
    return [e for e in (x.strip() for x in entries) if e]


def apply_visibility(params):
    """Spread a parameter's `+` (following), `-` (preceding) or `*` (both) marker to the notes, separators
    and links next to it, so a hidden control does not leave its explanation behind."""
    for i, p in enumerate(params):
        mark = getattr(p, "propagate", "")
        if not mark:
            continue
        for step, wanted in ((1, "+*"), (-1, "-*")):
            if mark not in wanted:
                continue
            j = i + step
            while 0 <= j < len(params) and not params[j].has_value:
                params[j].visibility = p.visibility
                j += step
    return params


def parse_param_block(text):
    return apply_visibility([p for p in (parse_param_line(e) for e in split_param_block(text)) if p is not None])


def parse_definitions(text, source=""):
    """Parse the `#@gui` lines of a G'MIC definition file into FilterDef objects."""
    filters, current = [], None
    for raw in text.splitlines():
        m = _GUI_RE.match(raw.rstrip("\r\n"))
        if not m:
            continue
        body = m.group(1).strip()
        if body.startswith(":"):                       # parameter of the current filter
            if current is None:
                continue
            param = parse_param_line(body[1:])
            if param is not None:
                current.params.append(param)
            continue
        current = None
        head, sep, spec = body.partition(" : ")
        if not sep:
            head, sep, spec = body.partition(":")
        if not sep:                                    # category line, nothing to run
            continue
        cmd = spec.split(",")[0].strip().lstrip("-")
        if not COMMAND_RE.match(cmd):
            continue
        preview, factor = _parse_preview_spec(spec.partition(",")[2])
        parts = [_TAG_RE.sub("", s).strip().lstrip("_").strip() for s in head.split(" > ")]
        parts = [s for s in parts if s]
        if not parts:
            continue
        current = FilterDef(path=tuple(parts[:-1]), name=parts[-1], command=cmd, preview_command=useful_preview(cmd, preview))
        if factor is not None:
            current.preview_factor = factor
        filters.append(current)
    for f in filters:
        apply_visibility(f.params)
    return filters


def find_definition_files(explicit=""):
    """Newest G'MIC `update*.gmic` cache (or the explicitly configured file)."""
    if explicit and os.path.isfile(explicit):
        return [explicit]
    best, best_ver = None, -1
    for folder in paths.gmic_config_dirs():
        for cand in glob.glob(os.path.join(folder, "update*.gmic")):
            m = _UPDATE_RE.search(cand)
            ver = int(m.group(1)) if m else 0
            if ver > best_ver:
                best, best_ver = cand, ver
    return [best] if best else []


class FilterIndex:
    def __init__(self, filters=()):
        self.filters = list(filters)
        self.by_id = {}
        for f in self.filters:
            self.by_id.setdefault(f.id, f)

    @classmethod
    def load(cls, explicit_definitions=""):
        """Catalogue = small built-in set + (explicit file | Krita's G'MIC-Qt cache | G'MIC update file)."""
        from . import gmicqt_cache
        filters = []
        with open(BUILTIN_FILE, "r", encoding="utf-8") as fh:
            filters += parse_definitions(fh.read(), BUILTIN_FILE)
        sources = []
        if explicit_definitions and os.path.isfile(explicit_definitions):
            candidates = [explicit_definitions]
        else:
            cached, cache_path = gmicqt_cache.load()
            if cached:
                filters += cached
                sources.append(cache_path)
            candidates = [] if cached else find_definition_files()
        for path in candidates:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    filters += parse_definitions(fh.read(), path)
                sources.append(path)
            except OSError:
                pass
        index = cls(filters)
        index.sources = sources
        return index

    sources = ()

    def get(self, filter_id):
        return self.by_id.get(filter_id)

    def search(self, query, limit=200):
        tokens = [t for t in query.lower().split() if t]
        if not tokens:
            return []
        scored = []
        for f in self.by_id.values():
            hay = f.id.lower()
            name = f.name.lower()
            score = 0
            for t in tokens:
                if t in name:
                    score += 3 if name.startswith(t) else 2
                elif t in hay:
                    score += 1
                elif _subsequence(t, name):
                    score += 0.5
                else:
                    score = -1
                    break
            if score > 0:
                scored.append((-score, len(f.name), f.id, f))
        scored.sort(key=lambda s: s[:3])
        return [s[3] for s in scored[:limit]]


def _subsequence(needle, hay):
    it = iter(hay)
    return all(c in it for c in needle)
