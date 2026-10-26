"""A list that supports multi-selection, with the Delete key and a ready-made button row."""
from .compat import QtCore, QtWidgets, enum, pyqtSignal

W = QtWidgets
_DELETE = (enum(QtCore.Qt, "Key", "Key_Delete"), enum(QtCore.Qt, "Key", "Key_Backspace"))


class ActionList(W.QListWidget):
    """Shift/Ctrl-click to select several entries, Ctrl+A for all; the Delete key asks to remove them."""
    deleteRequested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setSelectionMode(enum(W.QAbstractItemView, "SelectionMode", "ExtendedSelection"))
        self.setContextMenuPolicy(enum(QtCore.Qt, "ContextMenuPolicy", "CustomContextMenu"))

    def keyPressEvent(self, event):
        if event.key() in _DELETE and self.selectedItems():
            self.deleteRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def selected_keys(self, role):
        return [i.data(role) for i in self.selectedItems() if i.data(role)]


def batch_buttons(listw, remove_text):
    """('Select all' and '<remove_text>' buttons wired to `listw`, as one layout, plus the remove button)."""
    row = W.QHBoxLayout()
    select_all = W.QPushButton("Select all")
    remove = W.QPushButton(remove_text)
    remove.setEnabled(False)
    row.addWidget(select_all)
    row.addWidget(remove, 1)
    select_all.clicked.connect(listw.selectAll)

    def refresh():
        n = len(listw.selectedItems())
        remove.setEnabled(n > 0)
        remove.setText("%s (%d)" % (remove_text, n) if n else remove_text)

    listw.itemSelectionChanged.connect(refresh)
    return row, select_all, remove
