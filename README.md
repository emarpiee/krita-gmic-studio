# G'MIC Studio (Krita plugin)

A docker-based, non-blocking alternative to Krita's modal G'MIC-Qt dialog. Pure Python (PyQt5 for Krita 5, PyQt6 for Krita 6, same code), so one zip works on Windows, macOS and Linux. Works offline.

It runs the standalone `gmic` program in a background process and reads the filter catalogue from G'MIC itself, so Krita stays usable while you tune a filter.

## Features

**Preview**
- Live preview, debounced, with stale renders cancelled. Up to about 3 MP it runs the exact Apply job on the whole image, so the preview matches the result. Larger images follow each filter's declared preview zoom, as G'MIC-Qt does.
- Holding a slider or a point handle renders small, fast previews (two at a time, newest result wins) and one full render on release.
- The preview follows layer, selection and pixel changes by itself. Zoom with the wheel, pan by dragging, double-click for fit / 100%, checkerboard for transparency, hold-to-compare.
- G'MIC-Qt's 13 preview modes (split, checkered, duplicate), with a draggable divider. Filters that draw their own before/after view (for example Upscale [CNN2x]) get a draggable divider too.
- Draggable point handles that update the preview in real time.

**Parameters**
- Button parameters (one-shot), G'MIC's hidden / grayed-out markers, and values a filter reports back (a handle it moved, a hidden value) are all handled.
- Random filters share one seed between preview and Apply ("New random seed" re-rolls). Randomize with per-parameter locks.
- Out-of-range defaults are pulled into range, like G'MIC-Qt does.

**Organising**
- Search with a Recent button, Favorites, and the category tree keeps its open folders and scroll position.
- Presets form one library: every preset of every filter in a flat list, click to preview, double-click to apply, optional saved random seed, export/import. Presets can also be stored inside the Krita document (an annotation saved in the .kra), with explicit copy between the two.
- Each filter remembers its last values. "Repeat last filter" action.

**Applying**
- Three undoable apply modes: new layer, new layer + hide original, replace original (Krita cannot record plugin pixel edits for undo, but it does record layers and visibility). If Krita refuses to remove the original in replace mode, the original is hidden instead and the status line says so.
- Respects selections (including feathered edges), 8/16-bit integer RGBA, and passes transparency to filters as Krita does (RGBA), with an automatic RGB-only retry for filters that refuse an alpha channel.
- "Force transparent background" (off by default): one general rule detects the fill a filter paints over empty areas and clears it softly, keeping content the filter did not change.
- Filters that change the image size: the preview shows a dashed frame over the original, and a **Crop** toggle ("Keep the original size") cuts a bigger result to the original size around its center, or pads a smaller one. With Crop off, the result keeps its full size as a larger layer.

## Install

1. `python3 tools/build_zip.py` creates `dist/gmic_studio.zip`.
2. In Krita: *Tools > Scripts > Import Python Plugin from File*, pick the zip, enable it in the Python Plugin Manager, restart.
3. *Settings > Dockers > G'MIC Studio*.
4. Running filters needs the standalone `gmic` program, because Krita's bundled G'MIC-Qt cannot be driven by other plugins. *Settings > Get gmic...* downloads the official build on Windows and shows the package-manager command on Linux/macOS. Nothing is downloaded unless you click a download button.

**Filter list.** It is read from G'MIC's own `update*.gmic` file or from Krita's G'MIC-Qt cache (`~/.config/gmic/gmic_qt_filters.dat`; open G'MIC-Qt once to create it), whichever is newer. A cache older than gmic's definitions would have parameter lists that no longer fit the commands. *Settings > Update filter list* fetches the latest definitions (the only network use, and only when you ask).

## Layout

| Path | Role |
|---|---|
| `pykrita/gmic_studio.desktop` | Krita plugin descriptor |
| `pykrita/gmic_studio/core/` | Qt-free logic: gmic engine, `#@gui` parser, settings, presets, PNG I/O |
| `pykrita/gmic_studio/krita_bridge.py` | The only module that reads/writes Krita layers |
| `pykrita/gmic_studio/studio.py` | Controller: threads, previews, apply |
| `pykrita/gmic_studio/docker.py`, `param_form.py`, `preview_view.py`, `settings_dialog.py` | UI |
| `tests/` | pytest suite using a stub `krita` module and a fake `gmic` (many tests also use a real gmic when installed) |
| `tools/` | `build_zip.py`, `smoke_gmic.py`, `catalogue_check.py` |
| `HANDOFF.md`, `CLAUDE.md` | Current state and working notes for development sessions |

## Testing

```
pip install pytest PyQt6      # or PyQt5 with GMIC_TEST_QT=5
python -m pytest tests        # run with no DISPLAY
python tools/smoke_gmic.py    # needs a real gmic; validates the assumptions the fake cannot
```

`tools/catalogue_check.py` runs every filter with default values through the real gmic. It takes a long time and a lot of CPU: use `--jobs 1`, `--every N` or `--limit N` on a slow machine.

Do not import `tests/test_plugin.py` from a second test file (it hangs); add tests to it directly.

## Known limits

- Paint layers only; RGB(A) 8/16-bit integer only (F16/F32, CMYK, Lab and gray are refused with a message).
- Filters that need a plot window, a second layer or the internet do not work: they time out or fail with an explanatory message (for example Equation Plot [Parametric], Download External Data).
- gmic cannot take an empty argument, so an empty folder parameter is sent as `.`; empty text or file fields make some filters fail.
- Duplicate preview modes and filter-drawn splits need gmic to render the layout, so they update on release rather than while dragging.
- Each preview starts a gmic process (about 0.1 s of start-up on a fast machine), which limits how smooth live scrubbing can be.
- Tested against gmic 3.7.4 and PyQt6; the PyQt5 path is covered by the test suite but has not been run against a real Krita 5 recently.
