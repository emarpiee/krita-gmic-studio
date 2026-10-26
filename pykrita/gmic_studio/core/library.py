"""One place that decides which presets to show and in what order (used by every list in the UI)."""


def display_name(preset, index):
    """'Preset name · Filter name' (falls back to the stored filter id when the filter is not installed)."""
    fdef = index.get(preset["filter"]) if index is not None else None
    return "%s  ·  %s" % (preset["name"], fdef.name if fdef else preset["filter"].split(" > ")[-1])


def list_presets(store, index, query="", only_filter=None, scope=None):
    """Presets matching `query` (all words must appear in the name or the filter), best first.

    Without a query the most recently used come first, so what you reach for stays at the top.
    `only_filter` restricts the list to one filter id, `scope` to the library or to the open document.
    """
    tokens = query.lower().split()
    scored = []
    for p in store.all():
        if only_filter is not None and p["filter"] != only_filter:
            continue
        if scope is not None and p.get("scope", "library") != scope:
            continue
        fdef = index.get(p["filter"]) if index is not None else None
        name = p["name"].lower()
        hay = name + " " + p["filter"].lower() + (" " + fdef.name.lower() if fdef else "")
        if not all(t in hay for t in tokens):
            continue
        rank = sum(2 if name.startswith(t) else 1 if t in name else 0 for t in tokens)
        scored.append((-rank, "" if tokens else _invert(p.get("used", "")), p["name"].lower(), p))
    scored.sort(key=lambda s: s[:3])
    return [s[3] for s in scored]


def _invert(stamp):
    """Sort key that puts the newest timestamp first and never-used presets last."""
    return "".join(chr(0x10FFFF - ord(c)) for c in stamp) if stamp else "\U0010FFFF"


def list_favorites(index, favorite_ids, query=""):
    """Favorite filters (those still installed) matching `query`, by name."""
    tokens = query.lower().split()
    found = [index.get(i) for i in favorite_ids if index.get(i) is not None]
    found = [f for f in found if all(t in f.id.lower() for t in tokens)]
    return sorted(found, key=lambda f: (f.name.lower(), f.id))
