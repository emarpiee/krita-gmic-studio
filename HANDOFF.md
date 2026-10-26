# G'MIC Studio: hand-off

Krita docker plugin that improves G'MIC-Qt: live preview, undoable non-destructive apply, presets, favorites,
randomizer. It runs the standalone `gmic` CLI and reads the filter catalogue from Krita's G'MIC-Qt cache.
Works on Krita 5 (PyQt5) and Krita 6 (PyQt6). Offline.

## State
- Last commit is `74b5c9d`; everything from the "Fixed after the last commit" section below is uncommitted (plus this file and `CLAUDE.md`).
- Qt6: 187 tests pass (1 skipped); `tools/smoke_gmic.py` passes against gmic 3.7.4. **Qt5 was not run** (no PyQt5 on this machine now).
- No pytest/PyQt in the system Python any more: a venv with `--system-site-packages` + `pip install pytest PyQt6` was used. Recreate one in a scratch folder.
- `dist/gmic_studio.zip` is rebuilt. Not yet tried in real Krita.

## Layout
- `pykrita/gmic_studio/core/`: Qt-free engine (`engine.py` runs gmic via a generated script file), `filters.py`
  (definition parser, `Param`, visibility markers), presets, settings, pixels.
- `pykrita/gmic_studio/`: Qt layer: `docker.py`, `studio.py` (previews, apply, presets), `param_form.py`,
  `preview_view.py`, `krita_bridge.py` (the only place that touches Krita's document API).
- `tests/`: `conftest.py`, stubbed `krita`, fake gmic; many tests use the real gmic when installed.
- `tools/`: `build_zip.py`, `smoke_gmic.py`, `catalogue_check.py` (runs every filter; use `--jobs 3`, nice it).

## Running tests
- Qt6: `python -m pytest tests -q`; Qt5: `GMIC_TEST_QT=5 python -m pytest tests -q`. Run with no `DISPLAY`.
- Do not import `test_plugin` from a second test file (it hangs); add tests to `tests/test_plugin.py`.
- `tests/test_plugin.py::test_docker_stays_narrow` caps the docker's minimum width at 340 px; buttons in the preview
  row have explicit minimum widths for that reason.

## Added most recently
- Point handles update the preview while dragging (small fast image, newest request wins, full render on release).
- Button parameters (one-shot value for the next run); hidden/grayed `_0`/`_1`/`_2` markers with `+ - *` spreading.
- Preview modes (13, like G'MIC-Qt): split and checkered drawn natively with a draggable divider; Duplicate modes
  come from gmic's `gui_split_preview` using the real filtered image (`engine.split_preview`).
- Filters that resize the image: dashed frame on the preview and a "Crop" toggle (setting `keep_size`) that cuts the
  result to the original size around its center. Preview-row buttons: mode, dice (new seed), Crop.
- Slider fixes: labels above sliders; very wide ranges use a slider window around the default.

## Fixed after the last commit
- Definitions: the G'MIC-Qt cache is ignored when `update*.gmic` is newer (stale parameter lists caused "wrong argument count");
  lines ending in ` : *` (input mode) are parsed again (3D Mesh, Montage, Morph... were dropped); a default outside its range is
  pulled into it like G'MIC-Qt does (Quick Copyright's Orientation `-180` rotated the text 180 degrees); `\251` stays an octal escape (the copyright sign).
- Crop (`keep_size`) keeps its original behavior on purpose (the user asked): a bigger result is cut around its center, a smaller one is padded. Do not exempt upscales.
- Filters report values back (`engine.read_status`, `ParamForm.apply_status`): handles and hidden values follow, no new preview.
- Apply includes a button whose press the shown preview was made with (`ParamForm.apply_values`).
- Filters whose own preview draws a before/after split (e.g. Upscale [CNN2x]) get a draggable divider: the job redefines gmic's `gui_split_preview`
  (copied from the update file, `engine.split_override`) so the position can be passed in; release = new preview.
- Holding a slider handle previews like dragging a point handle: small fast image, newest request wins, full render on release (`ParamForm.scrubbing`). Up to `FAST_PARALLEL` (2) small renders run at once while scrubbing (gmic start-up, ~0.12 s, is the floor per render);
  results older than the one on screen are dropped (`Studio._runs`, `_shown_token`).
- Duplicate preview modes: draggable divider, and they accept a result of another size (gmic resizes both).
- Empty folder parameter is sent as `.` (gmic cannot take an empty argument at all). Timeouts and empty-field errors now explain themselves.

## Known limits
- Filters that need a plot window or the network time out (Equation Plot [Parametric], Download External Data): by design, no fix possible.
- Intarsia runs but fails inside its own code (writes HTML files, quoting error on its choice labels).
- The full catalogue audit was not re-run after these fixes (the machine is low spec; see `CLAUDE.md`). Spot checks only.
- 3D Mesh "inverted" report: not reproducible with current definitions; probably the stale-cache parameter mismatch.

## Working preferences (from the user)
- Keep fixes general; no per-filter patches. Replace weak or always-true test assertions with real checks.
- Keep test side trips short; the user wants the plugin fixed first. Never run commands that touch their system
  beyond the project and the scratch folder.
- Keep the docker narrow; the preview sits at the bottom; one-line status.
