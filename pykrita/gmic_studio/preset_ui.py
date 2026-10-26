"""Small dialogs for the preset library."""
from .compat import QtCore, QtWidgets, enum

W = QtWidgets


class PresetSaveDialog(W.QDialog):
    """Ask for a preset name, where to keep it (either place, or both) and whether to remember the random seed."""

    def __init__(self, parent, filter_name, name="", library_path="", can_document=False, scope="library",
                 has_seed_choice=True):
        super().__init__(parent)
        self.setWindowTitle("Save preset")
        self.setMinimumWidth(380)
        self._library_path = library_path
        form = W.QFormLayout(self)
        form.addRow(W.QLabel("Filter: <b>%s</b>" % _escape(filter_name)))
        self.edit = W.QLineEdit(name)
        self.edit.setPlaceholderText("Preset name")
        form.addRow("Name", self.edit)
        self.lib_check = W.QCheckBox("My library (available in every document)")
        self.doc_check = W.QCheckBox("This document (stored in the .kra file)")
        self.doc_check.setEnabled(can_document)
        self.lib_check.setChecked(scope in ("library", "both") or not can_document)
        self.doc_check.setChecked(can_document and scope in ("document", "both"))
        form.addRow("Keep it in", self.lib_check)
        form.addRow("", self.doc_check)
        self.seed = W.QCheckBox("Also remember the random seed (reproduce this exact look)")
        self.seed.setToolTip("Random filters give a different result each time. With the seed saved, "
                             "loading the preset brings back the same one.")
        self.seed.setVisible(has_seed_choice)
        form.addRow(self.seed)
        self.where = W.QLabel()
        self.where.setWordWrap(True)
        self.where.setTextInteractionFlags(enum(QtCore.Qt, "TextInteractionFlag", "TextSelectableByMouse"))
        form.addRow(self.where)
        self.buttons = W.QDialogButtonBox(enum(W.QDialogButtonBox, "StandardButton", "Save")
                                          | enum(W.QDialogButtonBox, "StandardButton", "Cancel"))
        self.buttons.accepted.connect(self._ok)
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)
        self.lib_check.toggled.connect(self._refresh)
        self.doc_check.toggled.connect(self._refresh)
        self._refresh()

    def _refresh(self, *_ignored):
        lines = []
        if self.lib_check.isChecked():
            lines.append("Library: %s" % self._library_path)
        if self.doc_check.isChecked():
            lines.append("Document: stored inside the .kra file, so it travels with it (save the file to keep it).")
        self.where.setText(_escape("\n".join(lines)) if lines else "Tick at least one place to keep the preset.")
        save = self.buttons.button(enum(W.QDialogButtonBox, "StandardButton", "Save"))
        if save is not None:
            save.setEnabled(bool(lines))

    def _ok(self):
        if self.edit.text().strip() and self.scopes():
            self.accept()
        else:
            self.edit.setFocus()

    def name(self):
        return self.edit.text().strip()

    def scopes(self):
        """The places ticked, library first."""
        return (["library"] if self.lib_check.isChecked() else []) + (["document"] if self.doc_check.isChecked() else [])

    def lock_seed(self):
        return (not self.seed.isHidden()) and self.seed.isChecked()


def _escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
