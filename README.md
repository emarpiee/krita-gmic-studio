# G'MIC Studio

A Krita plugin that gives you G'MIC filters in a docker panel, with a live preview.

> **Note:** I made this for my own use. It may not be maintained in the long run. See [About this project](#about-this-project).

Krita's built-in G'MIC window blocks Krita while it is open. G'MIC Studio is a docker instead, so you can keep working in Krita while you try a filter. It works offline.

## Status

- **Tested on Linux only** (Krita 6, gmic 3.7.4).
- **Windows and macOS have never been tested.** The code is written for them, but expect problems. If you try it there, run `python tools/smoke_gmic.py` first. It checks that your `gmic` works.
- The code supports Krita 5 (PyQt5) and Krita 6 (PyQt6). Krita 5 has not been tried recently.

## Features

**Preview**
- The preview updates while you change a setting (live preview).
- For images up to about 3 megapixels, the preview is the exact same job as Apply, on the whole image. For bigger images, it follows each filter's own preview setting, like G'MIC-Qt.
- When you hold a slider or a point handle, the preview shows small, fast images. When you let go, it makes one full-quality image.
- Zoom with the mouse wheel, move the picture by dragging, double-click for "fit" or 100%. A checkerboard shows transparent areas. Hold "Compare" to see the original.
- 13 preview modes from G'MIC-Qt (split, checkered, duplicate). Every split has a divider you can drag. Filters that draw their own before/after view, like Upscale [CNN2x], also get a draggable divider.
- Point parameters appear as handles on the preview. Drag them to move the point.

**Parameters**
- Supports button parameters, hidden and grayed-out parameters, and values that a filter sends back (for example, a point the filter moved).
- Random filters use the same random seed in the preview and in Apply. Use the dice button to get a new seed.
- "Randomize" can lock the parameters you want to keep.

**Organizing**
- Search box, a "Recent" list, Favorites, and a category tree that remembers which folders are open.
- Presets are in one list for all filters. Click a preset to preview it, double-click to apply it. You can export and import presets. You can also save presets inside the Krita document.
- Each filter remembers its last values. There is a "Repeat last filter" action.

**Applying**
- Three apply modes, all undoable with Ctrl+Z:
  - *New layer*: the result goes on a new layer above.
  - *New layer, hide original*: same, and the original layer is hidden.
  - *Replace original*: the result takes the place of the original layer.
- Works with selections (also soft edges) and with 8-bit and 16-bit RGBA layers.
- "Force transparent background" (off by default) removes the background color that some filters paint over empty areas.
- Some filters change the image size. The preview then shows a dashed frame for the original size. The **Crop** button ("Keep the original size") cuts a bigger result to the original size, around the center. A smaller result is padded. If Crop is off, the result keeps its full size as a larger layer.

## Requirements

- Krita 5 or 6 with Python plugins enabled.
- The standalone `gmic` program. See [Why a separate gmic?](#why-a-separate-gmic) below.

## Install

1. Build the plugin file: `python3 tools/build_zip.py`. This creates `dist/gmic_studio.zip`.
2. In Krita, go to *Tools > Scripts > Import Python Plugin from File* and choose the zip.
3. Enable "G'MIC Studio" in the Python Plugin Manager and restart Krita.
4. Open the docker: *Settings > Dockers > G'MIC Studio*.
5. If the docker says "gmic not found", click the gear button, then *Get gmic...*.
   - **Windows:** the plugin can download the official build for you, but only when you click the button.
   - **Linux and macOS:** it shows the install command for your system (for example `sudo dnf install gmic` or `brew install gmic`).

## How to use

1. Open an image and select a paint layer. If you want, make a selection.
2. Type in the search box, or open a category, and click a filter.
3. Change the parameters. With "Live" checked, the preview updates by itself. Use the refresh button to update it by hand.
4. Choose what Apply does in the box next to the Apply button (new layer, hide original, or replace original).
5. Click **Apply**. Use **Stop** to cancel a filter that is still running.

The filter list comes from G'MIC's own `update*.gmic` file or from Krita's G'MIC-Qt cache (`~/.config/gmic/gmic_qt_filters.dat`), whichever is newer. *Settings > Update filter list* downloads the newest definitions. This is the only time the plugin uses the internet, and only when you ask.

## Why a separate gmic?

Krita's own G'MIC cannot be used by other plugins:

- Krita includes G'MIC only as a plugin library (`krita_gmic_qt.so`). It has no `gmic` command-line program.
- The only way to start it is a C++ interface (`KisQmicPluginInterface::launch(image, headless)`) that only Krita can call. The `headless` option only means "repeat the last filter". There is no way to send your own command, get a preview back, or read an error message.

So this plugin runs the normal `gmic` program in the background. Krita's current source code builds G'MIC 3.7.4.1, and a recent `gmic` works fine. More details: Krita's [`ext_gmic`](https://github.com/KDE/krita/tree/master/3rdparty_plugins/ext_gmic) and [`plugins/extensions/qmic`](https://github.com/KDE/krita/tree/master/plugins/extensions/qmic).

## Known limits

- Only paint layers. Only RGB(A) with 8-bit or 16-bit integer values. Other layer types and color spaces show a message.
- Some filters cannot work. They need a plot window, a second layer, or the internet (for example *Equation Plot [Parametric]* and *Download External Data*). They time out or show an error message.
- `gmic` cannot receive an empty value. An empty folder field is sent as `.` (the current folder). Some filters with an empty text or file field fail.
- Duplicate preview modes, and splits drawn by a filter, update when you let go of the divider, not while you drag.
- Every preview starts a new `gmic` process. This takes about 0.1 seconds on a fast computer, so live previews can look a little choppy.

## For developers

**Folders**

| Path | What it does |
|---|---|
| `pykrita/gmic_studio.desktop` | Krita plugin description |
| `pykrita/gmic_studio/core/` | Logic without Qt: gmic engine, filter definition parser, settings, presets, PNG files |
| `pykrita/gmic_studio/krita_bridge.py` | The only file that reads and writes Krita layers |
| `pykrita/gmic_studio/studio.py` | Controller: background jobs, previews, apply |
| `pykrita/gmic_studio/docker.py`, `param_form.py`, `preview_view.py`, `settings_dialog.py` | User interface |
| `tests/` | Tests, using a fake `krita` module and a fake `gmic` (many tests also use a real `gmic` if installed) |
| `tools/` | `build_zip.py`, `smoke_gmic.py`, `catalogue_check.py` |
| `HANDOFF.md`, `CLAUDE.md` | Current state and working notes for development sessions |

**Tests**

```
pip install pytest PyQt6      # for Qt 5: install PyQt5 and set GMIC_TEST_QT=5
python -m pytest tests        # run without a display (no DISPLAY variable)
python tools/smoke_gmic.py    # needs a real gmic; checks what the fake gmic cannot
```

- `tools/catalogue_check.py` runs every filter with its default values through the real `gmic`. This takes a long time and a lot of CPU. On a slow computer, use `--jobs 1` and `--every N` or `--limit N`.
- Do not import `tests/test_plugin.py` from another test file (it hangs). Add new tests directly to it.

## About this project

I made this for my own use. Krita's built-in G'MIC window is not very intuitive, and I wanted something easier for me to work with, so this started as an accessibility tool for my personal projects. It may not be maintained in the long run.

It was built with the help of Claude Sonnet 5.5.

**A note on AI.** I am against AI-generated images. I am fine with AI-assisted tools that help an artist's workflow. AI is not meant to replace me as an artist. I use it to help me make tools that lessen the work of an artist, without ever touching the actual art. This plugin only runs G'MIC filters that you choose, on art that you made.
