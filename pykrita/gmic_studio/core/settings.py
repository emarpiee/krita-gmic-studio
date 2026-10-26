"""User settings, favorites and recents. Everything is stored locally as JSON (presets: see presets.py)."""
import os

from . import jsonstore, paths, presets

DEFAULTS = {
    "gmic_path": "",            # empty = auto-detect
    "definitions_path": "",     # empty = auto-detect update*.gmic
    "transparency": "auto",     # "auto" / "filter" (RGBA goes through the filter, like Krita) / "preserve" (RGB only)
    "transparent_bg": False,    # "Force transparent background": clear the fill a filter paints over empty areas (off: filters have own options)
    "result_mode": "new_layer", # "new_layer" | "hide" (new layer, original hidden) | "replace" (original removed)
    "preset_scope": "library",  # where "Save preset" saves by default: "library" or "document"
    "presets_path": "",         # empty = presets.json in the plugin's config folder
    "preview_size": 384,        # px of the 1:1 preview crop (big images, filters that need actual size)
    "preview_full_mp": 3.0,     # up to this many megapixels the preview renders the whole image, like Apply
    "preview_delay_ms": 400,
    "live_preview": True,
    "keep_size": False,         # crop (or pad) results the filter resized back to the original size, centered
    "preview_mode": 0,          # 0 = the filtered image; 1-12: split / duplicate / checkered views (G'MIC-Qt's numbers)
    "preview_split": [50.0, 50.0],
    "fast_slow_previews": True, # filters whose preview was slow get smaller previews (Refresh renders full size)
    "slow_preview_s": 6.0,      # ...where "slow" is more than this many seconds
    "slow_filters": [],
    "watch_layer": True,        # refresh the preview by itself when the layer, selection or pixels change
    "favorites": [],
    "recents": [],
    "last_filter": "",
    "last_values": {},
    "filter_values": {},        # filter id -> the values it was last applied with
}
MAX_RECENTS = 12


class Settings:
    def __init__(self, folder=None):
        self._folder = folder or paths.config_dir()
        self._file = os.path.join(self._folder, "settings.json")
        self._data = dict(DEFAULTS)
        self._data.update(jsonstore.load(self._file, {}))

    # -- plain values -------------------------------------------------
    def get(self, key):
        return self._data.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        self._data[key] = value
        self.save()

    def save(self):
        jsonstore.save(self._file, self._data)

    # -- favorites / recents -----------------------------------------
    def is_favorite(self, filter_id):
        return filter_id in self._data["favorites"]

    def toggle_favorite(self, filter_id):
        favs = list(self._data["favorites"])
        if filter_id in favs:
            favs.remove(filter_id)
        else:
            favs.append(filter_id)
        self.set("favorites", favs)
        return filter_id in favs

    def remove_favorites(self, filter_ids):
        """Remove several favorites with one write. Returns how many were favorites."""
        drop = set(filter_ids)
        keep = [f for f in self._data["favorites"] if f not in drop]
        removed = len(self._data["favorites"]) - len(keep)
        if removed:
            self.set("favorites", keep)
        return removed

    def note_preview_time(self, filter_id, seconds):
        """Remember filters whose whole-image preview was slow (or no longer is)."""
        slow = [f for f in self._data.get("slow_filters", []) if f != filter_id]
        if seconds > float(self._data.get("slow_preview_s", 6.0)):
            slow.append(filter_id)
        if slow != self._data.get("slow_filters", []):
            self.set("slow_filters", slow[-200:])

    def is_slow(self, filter_id):
        return bool(self._data.get("fast_slow_previews", True)) and filter_id in self._data.get("slow_filters", [])

    def push_recent(self, filter_id):
        recents = [r for r in self._data["recents"] if r != filter_id]
        recents.insert(0, filter_id)
        self.set("recents", recents[:MAX_RECENTS])

    def remember_last(self, filter_id, values):
        self._data["last_filter"] = filter_id
        self._data["last_values"] = values
        remembered = dict(self._data.get("filter_values") or {})
        remembered[filter_id] = values
        self._data["filter_values"] = remembered
        self.push_recent(filter_id)

    def values_for(self, filter_id):
        """The values this filter was last applied with, or None."""
        return (self._data.get("filter_values") or {}).get(filter_id)

    def presets_file(self):
        custom = (self._data.get("presets_path") or "").strip()
        return custom or presets.default_path(self._folder)
