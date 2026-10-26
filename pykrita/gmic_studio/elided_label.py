"""A one-line label that shortens long text with an ellipsis (the full text is the tooltip), so it never changes height."""
from .compat import QtCore, QtWidgets, enum


class ElidedLabel(QtWidgets.QLabel):
    def __init__(self):
        super().__init__("")
        self._full = ""
        self.setSizePolicy(enum(QtWidgets.QSizePolicy, "Policy", "Ignored"), enum(QtWidgets.QSizePolicy, "Policy", "Fixed"))
        self.setTextInteractionFlags(enum(QtCore.Qt, "TextInteractionFlag", "TextSelectableByMouse"))
        self.setFixedHeight(self.fontMetrics().lineSpacing() + 4)

    def set_full_text(self, text):
        self._full = text
        self.setToolTip(text)
        self._elide()

    def full_text(self):
        return self._full

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        if not self.isVisible():                       # no real width yet: keep the whole text
            self.setText(self._full)
            return
        self.setText(self.fontMetrics().elidedText(self._full, enum(QtCore.Qt, "TextElideMode", "ElideRight"),
                                                   max(10, self.width() - 4)))
