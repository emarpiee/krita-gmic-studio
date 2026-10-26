"""Preset library: named parameter sets for any filter, stored as one JSON file.

A preset remembers the filter, its parameter values and (optionally) the random seed, so a random filter
can be saved as exactly the look you liked. The file lives in the plugin's config folder by default, or
wherever the user points it (for example a synced folder). Presets can be exported and imported to share.
"""
import os
import shutil
import time
import uuid

from . import jsonstore

VERSION = 2
SHARE_KIND = "gmic-studio-presets"


def _clean(data):
    """Validate one preset dict loaded from disk; returns it or None."""
    if not isinstance(data, dict):
        return None
    name, fid, values = data.get("name"), data.get("filter"), data.get("values")
    if not (isinstance(name, str) and name.strip() and isinstance(fid, str) and fid and isinstance(values, dict)):
        return None
    seed = data.get("seed")
    return {
        "id": data.get("id") if isinstance(data.get("id"), str) and data.get("id") else uuid.uuid4().hex,
        "name": name.strip(),
        "filter": fid,
        "values": values,
        "seed": seed if isinstance(seed, int) and not isinstance(seed, bool) else None,
        "created": data.get("created") if isinstance(data.get("created"), str) else time.strftime("%Y-%m-%dT%H:%M"),
        "used": data.get("used") if isinstance(data.get("used"), str) else "",
    }


def _migrate_v1(data):
    """Old format: {filter_id: {preset_name: values}}."""
    out = []
    for fid, named in data.items():
        if isinstance(named, dict):
            for name, values in named.items():
                p = _clean({"name": name, "filter": fid, "values": values})
                if p:
                    out.append(p)
    return out


class FileBackend:
    """Presets kept in a JSON file (the user's library)."""

    def __init__(self, path):
        self.path = path

    def load(self):
        return jsonstore.load(self.path, {})

    def save(self, data):
        jsonstore.save(self.path, data)

    def keep_old_format(self):
        shutil.copyfile(self.path, self.path + ".v1.bak")


class CallbackBackend:
    """Presets kept wherever `read()` / `write(data)` put them (the open Krita document)."""
    path = ""

    def __init__(self, read, write):
        self._read, self._write = read, write

    def load(self):
        try:
            data = self._read()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data):
        self._write(data)


class PresetStore:
    def __init__(self, source):
        self._backend = FileBackend(source) if isinstance(source, str) else source
        self.path = getattr(self._backend, "path", "")
        self._items = []
        self.load()

    # -- persistence ----------------------------------------------------
    def load(self):
        data = self._backend.load()
        if data.get("version") == VERSION and isinstance(data.get("presets"), list):
            self._items = [p for p in (_clean(x) for x in data["presets"]) if p]
        elif data and "version" not in data:
            self._items = _migrate_v1(data)
            if self._items:
                try:
                    if hasattr(self._backend, "keep_old_format"):
                        self._backend.keep_old_format()
                    self.save()
                except OSError:
                    pass
        else:
            self._items = []

    def save(self):
        self._backend.save({"version": VERSION, "presets": self._items})

    # -- queries --------------------------------------------------------
    def all(self):
        return sorted(self._items, key=lambda p: (p["name"].lower(), p["filter"]))

    def for_filter(self, fid):
        return [p for p in self.all() if p["filter"] == fid]

    def get(self, pid):
        return next((p for p in self._items if p["id"] == pid), None)

    def find(self, fid, name):
        low = name.strip().lower()
        return next((p for p in self._items if p["filter"] == fid and p["name"].lower() == low), None)

    def __len__(self):
        return len(self._items)

    # -- changes --------------------------------------------------------
    def save_preset(self, name, fid, values, seed=None):
        """Create a preset, or replace the one with the same name for that filter. Returns it."""
        name = name.strip()
        if not name:
            raise ValueError("A preset needs a name.")
        existing = self.find(fid, name)
        if existing:
            existing.update(values=dict(values), seed=seed, name=name)
            preset = existing
        else:
            preset = _clean({"name": name, "filter": fid, "values": dict(values), "seed": seed})
            self._items.append(preset)
        self.save()
        return preset

    def touch(self, pid):
        """Remember that a preset was just used, so recently used ones are listed first."""
        preset = self.get(pid)
        if preset is not None:
            preset["used"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.save()

    def rename(self, pid, name):
        preset, name = self.get(pid), name.strip()
        if preset is None or not name:
            return False
        clash = self.find(preset["filter"], name)
        if clash and clash["id"] != pid:
            return False
        preset["name"] = name
        self.save()
        return True

    def delete(self, pid):
        before = len(self._items)
        self._items = [p for p in self._items if p["id"] != pid]
        if len(self._items) != before:
            self.save()
            return True
        return False

    def delete_many(self, pids):
        """Delete several presets with one write. Returns how many existed."""
        wanted = set(pids)
        before = len(self._items)
        self._items = [p for p in self._items if p["id"] not in wanted]
        removed = before - len(self._items)
        if removed:
            self.save()
        return removed

    # -- sharing --------------------------------------------------------
    def export_file(self, path, ids=None):
        chosen = [p for p in self.all() if ids is None or p["id"] in ids]
        jsonstore.save(path, {"kind": SHARE_KIND, "version": VERSION, "presets": chosen})
        return len(chosen)

    def import_file(self, path):
        """Merge a shared file. Returns (added, replaced). Same filter + same name is replaced."""
        data = jsonstore.load(path, {})
        if data.get("kind") != SHARE_KIND or not isinstance(data.get("presets"), list):
            raise ValueError("This is not a G'MIC Studio presets file.")
        added = replaced = 0
        for raw in data["presets"]:
            p = _clean(raw)
            if not p:
                continue
            existing = self.find(p["filter"], p["name"])
            if existing:
                existing.update(values=p["values"], seed=p["seed"])
                replaced += 1
            else:
                p["id"] = uuid.uuid4().hex                   # never collide with local ids
                self._items.append(p)
                added += 1
        if added or replaced:
            self.save()
        return added, replaced


LIBRARY, DOCUMENT = "library", "document"


class PresetBook:
    """The user's library plus (when a document is open) the presets stored inside that document.

    Each preset lives in exactly one of them, so nothing overlaps; copying between them is explicit. The
    methods mirror PresetStore, with a `scope` argument where it matters (default: the library).
    """

    def __init__(self, library, document=None):
        self.library = library
        self.document = document

    @property
    def path(self):
        return self.library.path

    @property
    def has_document(self):
        return self.document is not None

    def set_document(self, store):
        self.document = store

    def _stores(self):
        return [(LIBRARY, self.library)] + ([(DOCUMENT, self.document)] if self.document is not None else [])

    def store(self, scope):
        return self.document if scope == DOCUMENT else self.library

    def scope_of(self, pid):
        return next((scope for scope, st in self._stores() if st.get(pid) is not None), None)

    # -- queries --------------------------------------------------------
    def all(self):
        items = [dict(p, scope=scope) for scope, st in self._stores() for p in st.all()]
        return sorted(items, key=lambda p: (p["name"].lower(), p["filter"], p["scope"] != LIBRARY))   # library first on a tie

    def for_filter(self, fid):
        return [p for p in self.all() if p["filter"] == fid]

    def get(self, pid):
        for scope, st in self._stores():
            p = st.get(pid)
            if p is not None:
                return dict(p, scope=scope)
        return None

    def find(self, fid, name, scope=LIBRARY):
        st = self.store(scope)
        p = st.find(fid, name) if st is not None else None
        return dict(p, scope=scope) if p is not None else None

    def __len__(self):
        return sum(len(st) for _scope, st in self._stores())

    # -- changes --------------------------------------------------------
    def save_preset(self, name, fid, values, seed=None, scope=LIBRARY):
        st = self.store(scope)
        if st is None:
            raise ValueError("No document is open to keep the preset in.")
        return dict(st.save_preset(name, fid, values, seed), scope=scope)

    def _first(self, pid, action):
        for _scope, st in self._stores():
            if st.get(pid) is not None:
                return action(st)
        return False

    def rename(self, pid, name):
        return self._first(pid, lambda st: st.rename(pid, name))

    def touch(self, pid):
        self._first(pid, lambda st: st.touch(pid))

    def delete(self, pid):
        return bool(self.delete_many([pid]))

    def delete_many(self, pids):
        pids = list(pids)
        return sum(st.delete_many(pids) for _scope, st in self._stores())

    def copy(self, pid, scope):
        """Copy a preset into `scope` (replacing a same-named one there). Returns the copy, or None."""
        p = self.get(pid)
        if p is None or p["scope"] == scope or self.store(scope) is None:
            return None
        return self.save_preset(p["name"], p["filter"], p["values"], p["seed"], scope)

    # -- sharing (files) ---------------------------------------------------
    def export_file(self, path, ids=None):
        chosen = [{k: v for k, v in p.items() if k != "scope"} for p in self.all() if ids is None or p["id"] in ids]
        jsonstore.save(path, {"kind": SHARE_KIND, "version": VERSION, "presets": chosen})
        return len(chosen)

    def import_file(self, path, scope=LIBRARY):
        st = self.store(scope)
        if st is None:
            raise ValueError("No document is open to import the presets into.")
        return st.import_file(path)


def default_path(config_dir):
    return os.path.join(config_dir, "presets.json")
