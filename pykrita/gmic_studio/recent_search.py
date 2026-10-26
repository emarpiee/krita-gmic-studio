"""The search box and the Recent menu next to it."""
from .compat import QtCore, QtWidgets, enum, pyqtSignal

_ESC = enum(QtCore.Qt, "Key", "Key_Escape")


class SearchEdit(QtWidgets.QLineEdit):
    """Search box that is easy to leave: Esc clears it, and Esc on an empty box gives the focus back."""

    def keyPressEvent(self, event):
        if event.key() == _ESC:
            if self.text():
                self.clear()
            else:
                self.clearFocus()
            event.accept()
            return
        super().keyPressEvent(event)


class RecentButton(QtWidgets.QToolButton):
    """'Recent' button with a menu of recently used filters. It only opens when clicked and closes like any menu."""
    recentChosen = pyqtSignal(str)            # key (filter id) of the chosen recent filter
    clearRequested = pyqtSignal()             # "Clear recent filters" was chosen

    def __init__(self):
        super().__init__()
        self.setText("Recent")
        self.setToolTip("Filters you used recently")
        self.setPopupMode(enum(QtWidgets.QToolButton, "ToolButtonPopupMode", "InstantPopup"))
        self._menu = QtWidgets.QMenu(self)
        self.setMenu(self._menu)
        self._entries = []
        self.setEnabled(False)

    def set_recents(self, entries):
        """entries: [(label, key)], newest first."""
        self._entries = list(entries)
        self._menu.clear()
        for label, key in self._entries:
            self._menu.addAction(label).triggered.connect(lambda _checked=False, k=key: self.recentChosen.emit(k))
        if self._entries:
            self._menu.addSeparator()
            self._menu.addAction("Clear recent filters").triggered.connect(lambda _checked=False: self.clearRequested.emit())
        self.setEnabled(bool(self._entries))

    def labels(self):
        return [label for label, _key in self._entries]

    def choose(self, label):
        """Pick an entry by its label (what clicking its menu item does)."""
        for text, key in self._entries:
            if text == label:
                self.recentChosen.emit(key)
                return True
        return False
