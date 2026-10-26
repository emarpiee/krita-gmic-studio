"""One import surface for PyQt5 (Krita 5) and PyQt6 (Krita 6)."""
try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    from PyQt6.QtCore import pyqtSignal, pyqtSlot
    QT_MAJOR = 6
except ImportError:  # Krita 5
    from PyQt5 import QtCore, QtGui, QtWidgets
    from PyQt5.QtCore import pyqtSignal, pyqtSlot
    QT_MAJOR = 5

__all__ = ["QtCore", "QtGui", "QtWidgets", "pyqtSignal", "pyqtSlot", "QT_MAJOR", "enum",
           "qimage_from_rgba8", "qimage_to_bytes", "image_format"]


def enum(owner, group, name):
    """Resolve `Owner.Group.Name` (Qt6 style) or `Owner.Name` (Qt5 style)."""
    scoped = getattr(owner, group, None)
    if scoped is not None and hasattr(scoped, name):
        return getattr(scoped, name)
    return getattr(owner, name)


def image_format(name):
    return enum(QtGui.QImage, "Format", name)


def qimage_from_rgba8(data, width, height):
    img = QtGui.QImage(bytes(data), width, height, width * 4, image_format("Format_RGBA8888"))
    return img.copy()  # detach from the Python buffer


def qimage_to_bytes(img):
    """Raw bytes of a QImage whose rows are tightly packed (true for the RGBA formats we use)."""
    ptr = img.constBits()
    size = img.sizeInBytes() if hasattr(img, "sizeInBytes") else img.byteCount()
    if hasattr(ptr, "asstring"):
        return bytes(ptr.asstring(size))
    return bytes(ptr)[:size]
