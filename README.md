# G'MIC Studio (Krita plugin)

A docker-based, non-blocking alternative to Krita's modal G'MIC-Qt dialog. Pure Python (PyQt5 for Krita 5, PyQt6 for Krita 6, same code), so one zip works on Windows, macOS and Linux. Works offline.

## What it adds

- Docker UI: Krita stays usable while you tune a filter.
- Preview follows layer, selection and pixel changes by itself; errors are shown and logged (log file in the config folder).
- Presets can also be stored inside the Krita document (as an annotation saved in the .kra), separate from the library file, with explicit copy between the two.
- Zoomable preview (wheel, drag, double-click) with a transparency checkerboard; random filters share one seed between preview and Apply ("New random seed" re-rolls).
- Draggable point handles that update the preview in real time; button parameters; G'MIC's hidden / grayed-out parameter markers; G'MIC-Qt's preview modes (split, checkered, duplicate).
- Live preview, debounced, stale renders cancelled, hold-to-compare. Up to ~3 MP it runs the exact Apply job on the whole image (preview matches the result); larger images follow each filter's declared preview zoom, as G'MIC-Qt does.
- Search (with a Recent button; Esc leaves the box) plus Favorites; the filter list keeps its open categories and scroll position. Presets are one library on their own tab: every preset of every filter in a flat list (recently used first), click to preview, double-click to apply, previous/next buttons to step through, optional saved random seed, export/import, movable file. Each filter also remembers its last values; "Repeat last filter" action.
- "Force transparent background" (off by default, since many filters have their own option): one general rule detects the fill a filter paints over empty areas (flat or noisy) and clears it softly, keeping content the filter did not change. No per-filter code.
- Three undoable apply modes: new layer, new layer + hide original, replace original (Krita cannot record plugin pixel edits for undo, but it does record layers and visibility).
- Compact docker (about 320 px wide at minimum), preview below the parameters.
- Respects selections (including feathered edges), 8/16-bit integer RGBA, transparency passed to filters as in Krita (RGBA), with an automatic RGB-only retry for filters that refuse an alpha channel.
- Runs `gmic` in a background process, with Stop.

## Install

1. `python3 tools/build_zip.py` creates `dist/gmic_studio.zip`.
2. In Krita: *Tools > Scripts > Import Python Plugin from File*, pick the zip, enable it in the Python Plugin Manager, restart.
3. *Settings > Dockers > G'MIC Studio*. The filter list is Krita's own G'MIC-Qt catalogue, read from its cache (`~/.config/gmic/gmic_qt_filters.dat`; open G'MIC-Qt once to create it).
4. Running filters needs the standalone `gmic` program, because Krita's bundled G'MIC-Qt cannot be driven by other plugins. *Settings > Get gmic...* downloads the official build on Windows and shows the package-manager command on Linux/macOS. Nothing is downloaded unless you click a download button.

## Layout

| Path | Role |
|---|---|
| `pykrita/gmic_studio.desktop` | Krita plugin descriptor |
| `pykrita/gmic_studio/core/` | Qt-free logic: gmic engine, `#@gui` parser, settings, PNG I/O, archive import |
| `pykrita/gmic_studio/krita_bridge.py` | The only module that reads/writes Krita layers |
| `pykrita/gmic_studio/studio.py` | Controller: threads, previews, apply |
| `pykrita/gmic_studio/docker.py`, `param_form.py`, `settings_dialog.py` | UI |
| `tests/` | pytest suite using a stub `krita` module and a fake `gmic` |
| `tools/smoke_gmic.py` | Checks the pipeline against a real gmic |

## Testing

```
pip install pytest PyQt6      # or PyQt5 with GMIC_TEST_QT=5
python -m pytest tests
python tools/smoke_gmic.py    # needs a real gmic; validates the assumptions the fake cannot
```

## Status

The test suite passes under PyQt5 and PyQt6, but it uses a stub Krita and a fake gmic. Not yet verified against a real Krita or real gmic: run `tools/smoke_gmic.py` first (it checks the gmic command sequence, 16-bit PNG output and mask blending), then try the docker in Krita.

Known limits: paint layers only; RGB(A) U8/U16 only (F16/F32, CMYK, Lab, gray are refused with a message); filters that resize the image add a new layer instead of replacing; filters needing a second layer or internet are not supported; the preview crop follows the selection or layer centre, or the visible canvas centre if Krita reports it.
