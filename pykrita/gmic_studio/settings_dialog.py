"""Settings: where gmic lives, filter catalogue, and defaults. No network use unless a button says so."""
import os
import shutil

from .compat import QtCore, QtGui, QtWidgets, enum
from .core import getgmic, install, log

W = QtWidgets
DOWNLOAD_URL = "https://gmic.eu/download.html"
WHY = ("Why a separate gmic? Krita's built-in G'MIC is a plugin library that only Krita itself can run: other plugins "
       "cannot give it commands or read its previews. This plugin runs the standalone `gmic` program instead.")


class SettingsDialog(W.QDialog):
    def __init__(self, studio, parent=None):
        super().__init__(parent)
        self.studio = studio
        self.setWindowTitle("G'MIC Studio Settings")
        self.setMinimumWidth(460)
        s = studio.settings
        form = W.QFormLayout(self)

        self.engine_label = W.QLabel()
        self.engine_label.setWordWrap(True)
        form.addRow(self.engine_label)

        self.path = W.QLineEdit(s.get("gmic_path"))
        self.path.setPlaceholderText("Auto-detect")
        form.addRow("gmic program", self._with_buttons(self.path, [("Browse...", self._browse_exe)]))

        row = W.QHBoxLayout()
        for text, slot in (("Get gmic...", self._get_gmic), ("Import from archive...", self._import_archive)):
            b = W.QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        form.addRow(row)

        self.catalogue = W.QLabel()
        self.catalogue.setWordWrap(True)
        form.addRow(self.catalogue)

        self.defs = W.QLineEdit(s.get("definitions_path"))
        self.defs.setPlaceholderText("Auto-detect G'MIC's update*.gmic")
        form.addRow("Filter definitions", self._with_buttons(self.defs, [("Browse...", self._browse_defs)]))
        upd = W.QPushButton("Update filter list from gmic.eu (needs internet)")
        upd.clicked.connect(self._update_defs)
        form.addRow(upd)

        self.alpha = W.QComboBox()
        self.alpha.addItem("Automatic: pass transparency to the filter, like Krita (recommended)", "auto")
        self.alpha.addItem("Always pass transparency to the filter", "filter")
        self.alpha.addItem("Filter colors only, keep the original transparency", "preserve")
        self.alpha.setCurrentIndex(max(0, self.alpha.findData(s.get("transparency"))))
        form.addRow("Transparency", self.alpha)

        self.tbg = W.QCheckBox("Force transparent background: clear the fill a filter paints over empty areas")
        self.tbg.setToolTip("Off by default. Many filters have their own option for a transparent result;\n"
                            "this is the plugin's override for the ones that do not.")
        self.tbg.setChecked(bool(s.get("transparent_bg")))
        form.addRow("Background", self.tbg)

        self.output = W.QComboBox()
        self.output.addItem("New layer above (original kept; one undo)", "new_layer")
        self.output.addItem("New layer above, then hide the original (undo twice)", "hide")
        self.output.addItem("Replace the original layer (undo twice)", "replace")
        self.output.setCurrentIndex(max(0, self.output.findData(s.get("result_mode"))))
        form.addRow("Apply", self.output)

        self.presets_path = W.QLineEdit(s.get("presets_path"))
        self.presets_path.setPlaceholderText(studio.presets.path if not s.get("presets_path") else "")
        self.presets_path.setToolTip("The file your presets are saved in. Point it at a synced folder to share "
                                     "presets between computers.")
        box = self._with_buttons(self.presets_path, [("Browse...", self._browse_presets), ("Default", self._reset_presets)])
        form.addRow("Presets file", box)

        self.watch = W.QCheckBox("Refresh the preview by itself when the layer, selection or pixels change")
        self.watch.setChecked(bool(s.get("watch_layer")))
        form.addRow("Preview", self.watch)

        self.full_mp = W.QDoubleSpinBox()
        self.full_mp.setRange(0.1, 50.0)
        self.full_mp.setDecimals(1)
        self.full_mp.setSuffix(" megapixels")
        self.full_mp.setValue(float(s.get("preview_full_mp")))
        self.full_mp.setToolTip("Images up to this size are previewed in full, exactly like Apply. Lower it for faster "
                                "(but less exact) previews of big images.")
        form.addRow("Full-size preview up to", self.full_mp)

        self.fast = W.QCheckBox("Use smaller previews for filters slower than")
        self.fast.setChecked(bool(s.get("fast_slow_previews")))
        self.slow_s = W.QDoubleSpinBox()
        self.slow_s.setRange(1.0, 120.0)
        self.slow_s.setDecimals(0)
        self.slow_s.setSuffix(" s")
        self.slow_s.setValue(float(s.get("slow_preview_s")))
        self.forget = W.QPushButton("Forget slow filters (%d)" % len(s.get("slow_filters")))
        self.forget.clicked.connect(self._forget_slow)
        slow_row = W.QHBoxLayout()
        slow_row.addWidget(self.fast)
        slow_row.addWidget(self.slow_s)
        form.addRow("Slow filters", slow_row)
        form.addRow(self.forget)

        self.size = W.QSpinBox()
        self.size.setRange(64, 2048)
        self.size.setSuffix(" px")
        self.size.setValue(int(s.get("preview_size")))
        form.addRow("Preview size", self.size)

        self.delay = W.QSpinBox()
        self.delay.setRange(0, 5000)
        self.delay.setSuffix(" ms")
        self.delay.setValue(int(s.get("preview_delay_ms")))
        form.addRow("Preview delay", self.delay)

        logrow = W.QLabel(log.path())
        logrow.setTextInteractionFlags(enum(QtCore.Qt, "TextInteractionFlag", "TextSelectableByMouse"))
        logrow.setWordWrap(True)
        form.addRow("Log file", self._with_buttons(logrow, [("Open folder", self._open_log_folder)]))

        buttons = W.QDialogButtonBox(enum(W.QDialogButtonBox, "StandardButton", "Ok")
                                     | enum(W.QDialogButtonBox, "StandardButton", "Cancel"))
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._show_engine()

    @staticmethod
    def _with_buttons(edit, buttons):
        box = W.QWidget()
        lay = W.QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(edit, 1)
        for text, slot in buttons:
            b = W.QPushButton(text)
            b.clicked.connect(slot)
            lay.addWidget(b)
        return box

    def _show_engine(self):
        st = self.studio
        if st.exe:
            self.engine_label.setText("Using gmic %s\n%s" % (st.exe_version, st.exe))
        else:
            self.engine_label.setText("gmic was not found. The filter list below works without it, but running "
                                      "filters needs the gmic program. Click \"Get gmic...\". Nothing is "
                                      "downloaded unless you click a download button.")
        sources = getattr(st.index, "sources", [])
        count = len(st.index.filters)
        self.catalogue.setText("Filter list: %d filters from %s" % (count, ", ".join(sources) if sources else
                               "the small built-in set (open G'MIC once in Krita, or update the list, for the full catalogue)"))

    def _browse_exe(self):
        path = W.QFileDialog.getOpenFileName(self, "Select the gmic program")[0]
        if path:
            self.path.setText(path)

    def _forget_slow(self):
        self.studio.settings.set("slow_filters", [])
        self.forget.setText("Forget slow filters (0)")

    def _open_log_folder(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(os.path.dirname(log.path())))

    def _browse_presets(self):
        path = W.QFileDialog.getSaveFileName(self, "Where to keep presets", self.studio.presets.path,
                                             "G'MIC Studio presets (*.json)")[0]
        if path:
            self.presets_path.setText(path)

    def _reset_presets(self):
        self.presets_path.setText("")

    def _browse_defs(self):
        path = W.QFileDialog.getOpenFileName(self, "Select a G'MIC definitions file", "", "G'MIC (*.gmic);;All (*)")[0]
        if path:
            self.defs.setText(path)

    def _import_archive(self):
        path = W.QFileDialog.getOpenFileName(self, "Select the downloaded gmic archive", "",
                                             "Archives (*.zip *.tar *.tar.gz *.tgz *.tar.xz *.tar.bz2);;All (*)")[0]
        if not path:
            return
        try:
            self.path.setText(install.install_archive(path))
        except install.InstallError as exc:
            W.QMessageBox.warning(self, "G'MIC Studio", "Could not import: %s" % exc)

    def _get_gmic(self):
        dlg = GetGmicDialog(self.studio, self)
        dlg.exec()
        if dlg.installed_path:
            self.path.setText(dlg.installed_path)

    def _update_defs(self):
        self._accept(close=False)
        yes = enum(W.QMessageBox, "StandardButton", "Yes")
        no = enum(W.QMessageBox, "StandardButton", "No")
        answer = W.QMessageBox.question(self, "G'MIC Studio",
                                        "This runs `gmic update`, which downloads the latest filter definitions "
                                        "from the G'MIC servers. Continue?", yes | no)
        if answer == yes:
            self.studio.update_definitions()

    def _move_presets(self, s):
        """Switch the presets file; offer to carry the existing presets over to a new, empty location."""
        new = self.presets_path.text().strip()
        if new == (s.get("presets_path") or ""):
            return
        old_path, old_count = self.studio.presets.path, len(self.studio.presets)
        s.set("presets_path", new)
        target = s.presets_file()
        if old_count and not os.path.exists(target):
            yes = enum(W.QMessageBox, "StandardButton", "Yes")
            no = enum(W.QMessageBox, "StandardButton", "No")
            if W.QMessageBox.question(self, "G'MIC Studio", "Copy your %d presets to the new location?\n%s" % (old_count, target),
                                      yes | no) == yes:
                try:
                    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                    shutil.copyfile(old_path, target)
                except OSError as exc:
                    W.QMessageBox.warning(self, "G'MIC Studio", "Could not copy the presets: %s" % exc)
        self.studio.reload_presets()

    def _accept(self, close=True):
        s = self.studio.settings
        s.set("gmic_path", self.path.text().strip())
        s.set("definitions_path", self.defs.text().strip())
        s.set("transparency", self.alpha.currentData())
        s.set("transparent_bg", self.tbg.isChecked())
        s.set("watch_layer", self.watch.isChecked())
        s.set("preview_full_mp", self.full_mp.value())
        s.set("fast_slow_previews", self.fast.isChecked())
        s.set("slow_preview_s", self.slow_s.value())
        s.set("result_mode", self.output.currentData())
        self._move_presets(s)
        s.set("preview_size", self.size.value())
        s.set("preview_delay_ms", self.delay.value())
        self.studio.refresh()
        if close:
            self.accept()


class GetGmicDialog(W.QDialog):
    """Windows: download the official CLI zip. Elsewhere: show the right package-manager command."""

    def __init__(self, studio, parent=None):
        super().__init__(parent)
        self.studio = studio
        self.installed_path = None
        self._cancel = False
        self.setWindowTitle("Get gmic")
        self.setMinimumWidth(420)
        lay = W.QVBoxLayout(self)
        self.msg = W.QLabel()
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)
        self.progress = W.QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        lay.addWidget(self.progress)
        row = W.QHBoxLayout()
        self.action = W.QPushButton()
        close = W.QPushButton("Close")
        close.clicked.connect(self._close)
        row.addWidget(self.action)
        row.addStretch(1)
        row.addWidget(close)
        if getgmic.can_download():
            self.msg.setText(WHY + "\n\nDownloads the official command-line build (about 15 MB) from " + getgmic.PAGE_URL +
                             " (the exact file is picked when you click) and keeps it in this plugin's private folder. "
                             "Nothing else is installed on your system, and nothing is downloaded before you click.")
            self.action.setText("Download from gmic.eu")
            self.action.clicked.connect(self._download)
        else:
            text, cmd = getgmic.install_command()
            self.msg.setText(WHY + "\n\n" + text + ("\n\n" + cmd if cmd else "") +
                             "\n\nThen close this dialog; the plugin finds gmic on your PATH.")
            self.msg.setTextInteractionFlags(enum(QtCore.Qt, "TextInteractionFlag", "TextSelectableByMouse"))
            if cmd:
                self.action.setText("Copy command")
                self.action.clicked.connect(lambda: W.QApplication.clipboard().setText(cmd))
            else:
                self.action.setText("Open gmic.eu download page")
                self.action.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(DOWNLOAD_URL)))
        lay.addLayout(row)

    def _download(self):
        self.action.setEnabled(False)
        self.progress.show()
        self.msg.setText("Downloading...")

        def work():
            url = getgmic.windows_zip_url()
            archive = getgmic.download(url, cancelled=lambda: self._cancel)
            try:
                return install.install_archive(archive)
            finally:
                try:
                    os.unlink(archive)
                except OSError:
                    pass

        def done(result, error):
            try:
                self.progress.hide()
                if error is not None:
                    self.msg.setText("Download failed: %s" % error)
                    self.action.setEnabled(True)
                else:
                    self.installed_path = result
                    self.msg.setText("Installed: %s\nClick Close, then OK in Settings." % result)
            except RuntimeError:        # dialog was closed while downloading
                pass

        self.studio.run_async(work, done)

    def _close(self):
        self._cancel = True
        self.reject()
