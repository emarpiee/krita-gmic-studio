"""Controller shared by the docker and the menu actions: engine state, previews, applying."""
import os
import random
import shutil
import tempfile
import time

from . import krita_bridge as bridge
from .compat import QtCore, QtGui, enum, image_format, pyqtSignal, qimage_from_rgba8, qimage_to_bytes
from .core import engine, filters, log, pixels
from .core.presets import DOCUMENT, LIBRARY, CallbackBackend, PresetBook, PresetStore
from .core.settings import Settings

_instance = None
SIZE_TOLERANCE = 2        # px a filter may round the output size by before it counts as a resize
FAST_SIDE = 512           # longest side of the smaller preview used for filters that proved slow
THUMB_SIDE = 800          # longest side of a whole-image thumbnail preview


def _scale_rgba8(data, w, h, tw, th):
    img = qimage_from_rgba8(data, w, h).scaled(tw, th, enum(QtCore.Qt, "AspectRatioMode", "IgnoreAspectRatio"),
                                               enum(QtCore.Qt, "TransformationMode", "SmoothTransformation"))
    return qimage_to_bytes(img.convertToFormat(image_format("Format_RGBA8888")))


def _scale_gray8(data, w, h, tw, th):
    img = QtGui.QImage(bytes(data), w, h, w, image_format("Format_Grayscale8")).scaled(
        tw, th, enum(QtCore.Qt, "AspectRatioMode", "IgnoreAspectRatio"),
        enum(QtCore.Qt, "TransformationMode", "SmoothTransformation"))
    raw, stride = qimage_to_bytes(img), img.bytesPerLine()          # rows may be padded
    return b"".join(raw[y * stride:y * stride + tw] for y in range(th))


def get_studio():
    global _instance
    if _instance is None:
        _instance = Studio()
    return _instance


class _Thread(QtCore.QThread):
    """Runs a callable off the GUI thread; result/exception come back through `done`."""
    done = pyqtSignal(object, object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn(), None)
        except BaseException as exc:  # reported to the GUI, never raised in the thread
            self.done.emit(None, exc)


class Studio(QtCore.QObject):
    engineChanged = pyqtSignal()
    indexChanged = pyqtSignal()
    busyChanged = pyqtSignal(bool)
    status = pyqtSignal(str, bool)                 # message, is_error
    previewReady = pyqtSignal(object, object)      # before QImage, after QImage
    presetsChanged = pyqtSignal()
    previewBusy = pyqtSignal(bool)                 # a preview is being rendered

    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.presets = PresetBook(PresetStore(self.settings.presets_file()))
        self._doc_id = None
        self.index = filters.FilterIndex()
        self.exe = None
        self.exe_version = ""
        self._threads = set()
        self._preview_token = 0
        self._preview_runner = None
        self.last_result_resized = False             # the last preview's result had another size than the original (before any crop)
        self._pending_preview = None                 # newest request that arrived while an interactive one was rendering
        self._apply_runner = None
        self._busy = False
        self.preview_whole = False                   # the last preview covers the whole region (so point handles line up)
        self._bad_previews = set()                   # filters whose own preview command failed: use the normal one
        self.seed = random.randrange(1, 2 ** 31)     # shared by preview and apply, so they agree

    # -- background helper --------------------------------------------
    def _spawn(self, fn, callback):
        """Run fn() on a worker thread; callback(result, error) runs on the GUI thread afterwards."""
        thread = _Thread(fn)
        self._threads.add(thread)

        def release():                      # only once Qt says run() has really returned
            self._threads.discard(thread)
            thread.deleteLater()

        thread.done.connect(callback)
        thread.finished.connect(release)
        thread.start()

    def wait_for_threads(self, timeout_ms=10000):
        """Block until background work has finished (used when shutting down, and by the tests)."""
        for thread in list(self._threads):
            thread.wait(timeout_ms)

    # -- presets --------------------------------------------------------
    def reload_presets(self):
        """Re-open the library file (after the location setting changed)."""
        self.presets.library = PresetStore(self.settings.presets_file())
        self.presetsChanged.emit()

    def sync_document(self):
        """Follow the active document: its embedded presets appear as a second source (and go when it is closed)."""
        ident = bridge.document_id()
        if ident == self._doc_id:
            return False
        self._doc_id = ident
        store = None
        if ident is not None:
            try:
                store = PresetStore(CallbackBackend(*bridge.document_presets_io()))
            except Exception:
                log.get().exception("could not open the presets stored in the document")
        self.presets.set_document(store)
        self.presetsChanged.emit()
        return True

    def save_preset(self, name, fdef, values, lock_seed=False, scope=LIBRARY):
        preset = self.presets.save_preset(name, fdef.id, values, self.seed if lock_seed else None, scope)
        self.settings.set("preset_scope", scope)
        self.presetsChanged.emit()
        return preset

    def save_presets(self, name, fdef, values, lock_seed, scopes):
        """Save one preset into every place in `scopes`. Returns the presets (library first)."""
        made = [self.presets.save_preset(name, fdef.id, values, self.seed if lock_seed else None, scope) for scope in scopes]
        self.settings.set("preset_scope", "both" if len(scopes) > 1 else scopes[0])
        self.presetsChanged.emit()
        return made

    def copy_presets(self, pids, scope):
        """Copy presets into the library or into the open document. Returns how many were copied."""
        n = sum(1 for pid in pids if self.presets.copy(pid, scope) is not None)
        if n:
            self.presetsChanged.emit()
        return n

    def rename_preset(self, pid, name):
        ok = self.presets.rename(pid, name)
        if ok:
            self.presetsChanged.emit()
        return ok

    def delete_preset(self, pid):
        ok = self.presets.delete(pid)
        if ok:
            self.presetsChanged.emit()
        return ok

    def import_presets(self, path):
        counts = self.presets.import_file(path)
        self.presetsChanged.emit()
        return counts

    def delete_presets(self, pids):
        n = self.presets.delete_many(pids)
        if n:
            self.presetsChanged.emit()
        return n

    def run_async(self, fn, callback):
        """Run fn() off the GUI thread, then callback(result, error) on it."""
        self._spawn(fn, callback)

    # -- engine / index -------------------------------------------------
    def refresh(self):
        """Re-detect gmic and reload the filter list (both off the GUI thread)."""
        configured = self.settings.get("gmic_path")
        definitions = self.settings.get("definitions_path")

        def work():
            exe = engine.locate(configured)
            ver = engine.version(exe) if exe else ""
            return exe, ver, filters.FilterIndex.load(definitions)

        def done(result, error):
            if error is not None:
                self.exe, self.exe_version = None, ""
                self.status.emit("G'MIC check failed: %s" % error, True)
                self.engineChanged.emit()
                return
            self.exe, self.exe_version, self.index = result
            self.engineChanged.emit()
            self.indexChanged.emit()

        self._spawn(work, done)

    def update_definitions(self):
        """User-triggered download of G'MIC's filter catalogue (the only network access)."""
        if not self.exe:
            self.status.emit("Set up G'MIC first.", True)
            return
        exe = self.exe

        def done(result, error):
            if error is not None:
                self.status.emit("Update failed: %s" % error, True)
            else:
                self.status.emit("Filter definitions updated.", False)
                self.refresh()

        self.status.emit("Updating filter definitions...", False)
        self._spawn(lambda: engine.update_definitions(exe), done)

    # -- jobs -----------------------------------------------------------
    @property
    def busy(self):
        return self._busy

    def _set_busy(self, value):
        if value != self._busy:
            self._busy = value
            self.busyChanged.emit(value)

    def new_seed(self):
        """Re-roll the random seed (random filters then give a different result)."""
        self.seed = random.randrange(1, 2 ** 31)
        return self.seed

    def _make_job(self, fdef, values, w, h, data, depth, mask, preview=False):
        command, frags = fdef.command_line(values)
        if preview and fdef.preview_command and fdef.id not in self._bad_previews:
            command = fdef.preview_command            # draws preview-only extras (brush inset, split view...); never used by Apply
        extra_defs = self.settings.get("definitions_path")
        probes = None
        if self.settings.get("transparent_bg"):
            pb = 4 if depth == 8 else 8
            probes = pixels.background_probes(data, w, h, pb, 3 if depth == 8 else 6)    # 16-bit data is big-endian
        return engine.Job(command=command, arguments=frags, width=w, height=h, data=data,
                          depth=depth, mask=mask, alpha_mode=self.settings.get("transparency"), seed=self.seed,
                          bg_probes=probes,
                          definitions=extra_defs if extra_defs and os.path.isfile(extra_defs) else "")

    def _plan_preview(self, fdef, force_full=False, fast=False):
        """Choose what to render: (snapshot, w, h, rgba8, mask, description).

        Up to `preview_full_mp` megapixels the preview is the real job on the whole image, so it matches
        Apply. Bigger images follow the filter's own declared preview zoom (G'MIC-Qt's rule): zoom 0 needs
        a 1:1 crop, anything else works on a scaled-down whole image.
        """
        w, h = bridge.region_size()
        if not force_full and (fast or self.settings.is_slow(fdef.id)) and max(w, h) > FAST_SIDE:
            snap = bridge.grab()                          # this filter was slow before: preview a smaller copy
            k = FAST_SIDE / float(max(snap.w, snap.h))
            tw, th = max(1, int(round(snap.w * k))), max(1, int(round(snap.h * k)))
            mask = _scale_gray8(snap.mask, snap.w, snap.h, tw, th) if snap.mask is not None else None
            return (snap, tw, th, _scale_rgba8(bridge.to_preview_rgba8(snap), snap.w, snap.h, tw, th), mask,
                    "fast, smaller image; Refresh renders full size")
        if w * h <= float(self.settings.get("preview_full_mp")) * 1e6:
            snap = bridge.grab()
            return snap, snap.w, snap.h, bridge.to_preview_rgba8(snap), snap.mask, "whole image"
        if fdef.preview_factor == 0:
            snap = bridge.grab(limit=int(self.settings.get("preview_size")))
            return snap, snap.w, snap.h, bridge.to_preview_rgba8(snap), snap.mask, "1:1 crop"
        snap = bridge.grab()
        k = THUMB_SIDE / float(max(snap.w, snap.h))
        tw, th = max(1, int(round(snap.w * k))), max(1, int(round(snap.h * k)))
        mask = _scale_gray8(snap.mask, snap.w, snap.h, tw, th) if snap.mask is not None else None
        return snap, tw, th, _scale_rgba8(bridge.to_preview_rgba8(snap), snap.w, snap.h, tw, th), mask, "scaled image"

    def _unexpected(self, what, exc):
        """An error nobody planned for: log the traceback and tell the user, never fail silently."""
        log.get().exception("%s failed", what)
        self.status.emit("%s failed unexpectedly: %s  (details: %s)" % (what, exc, log.path()), True)
        self.previewBusy.emit(False)

    def request_preview(self, fdef, values, force_full=False, interactive=False):
        """Render a preview. `interactive` (a handle being dragged): use the small fast image, and instead of
        cancelling the render in progress let it finish and then render only the newest request."""
        try:
            self._request_preview(fdef, values, force_full, interactive)
        except Exception as exc:
            self._unexpected("Preview", exc)

    def _request_preview(self, fdef, values, force_full=False, interactive=False):
        if not self.exe or fdef is None:
            return
        if interactive and self._preview_runner is not None:
            self._pending_preview = (fdef, dict(values))
            return
        self.cancel_preview()
        try:
            snap, w, h, data, mask, kind = self._plan_preview(fdef, force_full, fast=interactive)
            job = self._make_job(fdef, values, w, h, data, 8, mask, preview=True)
        except (bridge.BridgeError, ValueError) as exc:
            self.status.emit(str(exc), True)
            return
        using_preview_cmd = job.command != fdef.command
        self._preview_token += 1
        token = self._preview_token
        runner = engine.Runner(self.exe, job, timeout=120)
        self._preview_runner = runner
        before = qimage_from_rgba8(job.data, w, h)
        self.status.emit("Previewing (%s)..." % kind, False)
        started = time.time()

        def done(result, error):
            try:
                if token != self._preview_token:
                    return                              # superseded by a newer request: another one is running
                self._preview_runner = None
                self.previewBusy.emit(False)
                if isinstance(error, engine.Cancelled):
                    return
                if error is not None and using_preview_cmd:
                    log.get().warning("preview command %s failed (%s); using %s instead", fdef.preview_command,
                                      str(error)[-200:], fdef.command)
                    self._bad_previews.add(fdef.id)
                    self._request_preview(fdef, values, force_full, interactive)       # once more, with the normal command
                    return
                if error is not None:
                    log.get().warning("preview failed: %s", error)
                    self.status.emit("Preview failed: %s" % _explain(error), True)
                    return
                try:
                    after = self._load_rgba8(result, w, h)
                except engine.GmicError as exc:
                    self.status.emit(str(exc), True)
                    return
                self.preview_whole = kind != "1:1 crop"
                self.previewReady.emit(before, after)
                took = time.time() - started
                note = ""
                if kind == "whole image":
                    was_slow = self.settings.is_slow(fdef.id)
                    self.settings.note_preview_time(fdef.id, took)
                    if self.settings.is_slow(fdef.id) and not was_slow:
                        note = " It took %.0f s, so live previews of this filter will use a smaller image; Refresh renders full size." % took
                self.status.emit("Preview ready: %s, %.1f s%s.%s" % (kind, took, ", selection active" if snap.selection else "", note), False)
            except Exception as exc:
                self._unexpected("Preview", exc)
            finally:
                if result:
                    shutil.rmtree(result[4], ignore_errors=True)      # also for stale or failed results
                pending, self._pending_preview = self._pending_preview, None
                if pending is not None and self._preview_runner is None:
                    self.request_preview(pending[0], pending[1], interactive=True)

        self.previewBusy.emit(True)
        self._spawn(runner.run, done)

    def render_split(self, before, after, mode, x, y, callback):
        """G'MIC's own layout for the "Duplicate ..." preview modes. callback(QImage or None) runs on the GUI thread."""
        if not self.exe:
            callback(None)
            return
        folder = tempfile.mkdtemp(prefix="gmic_studio_split_")
        before.save(os.path.join(folder, "before.png"))
        after.save(os.path.join(folder, "after.png"))
        exe = self.exe

        def work():
            return engine.split_preview(exe, os.path.join(folder, "before.png"), os.path.join(folder, "after.png"), mode, x, y)

        def done(path, error):
            try:
                img = QtGui.QImage(path) if error is None else None
                if error is not None:
                    log.get().warning("split preview failed: %s", error)
                callback(img.convertToFormat(image_format("Format_RGBA8888")) if img is not None and not img.isNull() else None)
            finally:
                shutil.rmtree(folder, ignore_errors=True)

        self._spawn(work, done)

    def cancel_preview(self):
        self._preview_token += 1
        self._pending_preview = None
        if self._preview_runner is not None:
            self._preview_runner.cancel()
            self._preview_runner = None
            self.previewBusy.emit(False)

    def _keep_size(self, img, width, height):
        """With "keep original size" on, a resized result is cut (or padded with transparency) to the original
        size around its center, exactly the part of it that sits over the original."""
        if self.settings.get("keep_size") and (img.width(), img.height()) != (width, height):
            return img.copy(engine.centered(img.width(), width), engine.centered(img.height(), height), width, height)
        return img

    def _load_rgba8(self, result, width, height):
        w, h, depth, path, _workdir = result
        img = QtGui.QImage()
        if not img.load(path):
            raise engine.GmicError("Could not read gmic's output image.")
        img = img.convertToFormat(image_format("Format_RGBA8888"))
        self.last_result_resized = (img.width(), img.height()) != (width, height)
        return self._keep_size(img, width, height)

    def apply(self, fdef, values):
        try:
            self._apply_now(fdef, values)
        except Exception as exc:
            self._set_busy(False)
            self._unexpected("Apply", exc)

    def _apply_now(self, fdef, values):
        if self._busy:
            self.status.emit("Another filter is still running.", True)
            return
        if not self.exe or fdef is None:
            self.status.emit("Set up G'MIC first.", True)
            return
        self.cancel_preview()
        try:
            snap = bridge.grab()
            if snap.depth == 16:
                data, depth = pixels.u16_native_to_be(snap.rgba), 16
            else:
                data, depth = snap.rgba, 8
            job = self._make_job(fdef, values, snap.w, snap.h, data, depth, snap.mask)
        except (bridge.BridgeError, ValueError) as exc:
            self.status.emit(str(exc), True)
            return
        runner = engine.Runner(self.exe, job)
        self._apply_runner = runner
        self._set_busy(True)
        self.status.emit("Applying %s%s..." % (fdef.name, " to the selection" if snap.selection else ""), False)
        mode = self.settings.get("result_mode")

        def done(result, error):
            self._apply_runner = None
            workdir = result[4] if result else None
            try:
                if isinstance(error, engine.Cancelled):
                    self.status.emit("Cancelled.", False)
                elif error is not None:
                    self.status.emit("Filter failed: %s" % _explain(error), True)
                else:
                    rgba, size = self._decode_result(result, snap)
                    notes = []
                    bridge.write(snap, rgba, mode, fdef.name, size, notes)
                    self.settings.remember_last(fdef.id, values)
                    if size != (snap.w, snap.h):
                        notes.insert(0, "The result has a new size (%dx%d), so it was added as a new layer." % size)
                    self.status.emit(" ".join(["Applied %s." % fdef.name] + notes), False)
            except (engine.GmicError, bridge.BridgeError) as exc:
                self.status.emit(str(exc), True)
            except Exception as exc:  # Krita API surprises must not leave the UI stuck busy
                log.get().exception("could not write the result of %s", fdef.name)
                self.status.emit("Could not write the result: %s  (details: %s)" % (exc, log.path()), True)
            finally:
                if workdir:
                    shutil.rmtree(workdir, ignore_errors=True)
                self._set_busy(False)

        self._spawn(runner.run, done)

    def _decode_result(self, result, snap):
        w, h, depth, path, _workdir = result
        if depth != snap.depth:
            raise engine.GmicError("gmic wrote a %d-bit image but %d-bit was expected; "
                                   "this gmic build may not support 16-bit PNG output." % (depth, snap.depth))
        img = QtGui.QImage()
        if not img.load(path):
            raise engine.GmicError("Could not read gmic's output image.")
        if snap.depth == 16:
            img = img.convertToFormat(image_format("Format_RGBA64"))
        else:
            img = img.convertToFormat(image_format("Format_RGBA8888"))
        img = self._keep_size(img, snap.w, snap.h)
        w, h = img.width(), img.height()
        data = qimage_to_bytes(img)
        if len(data) != w * h * 4 * (snap.depth // 8):
            raise engine.GmicError("Unexpected image buffer size from gmic.")
        if (w, h) != (snap.w, snap.h) and abs(w - snap.w) <= SIZE_TOLERANCE and abs(h - snap.h) <= SIZE_TOLERANCE:
            # Filters often round to even sizes; that is not a resize, keep the canvas size.
            data = pixels.fit_rgba(data, w, h, snap.w, snap.h, 4 * (snap.depth // 8))
            w, h = snap.w, snap.h
        return data, (w, h)

    def cancel_apply(self):
        if self._apply_runner is not None:
            self._apply_runner.cancel()

    def repeat_last(self):
        fid = self.settings.get("last_filter")
        fdef = self.index.get(fid)
        if fdef is None:
            self.status.emit("No previous filter to repeat (or the filter list is not loaded yet).", True)
            return
        self.apply(fdef, self.settings.get("last_values"))


def _explain(error):
    text = str(error)
    if "nknown command" in text or "nknown function" in text:
        return ("%s\nThis filter needs a newer gmic than the one installed. Update gmic, "
                "or use Settings > Update filter list." % text)
    return text
