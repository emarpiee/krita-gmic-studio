"""Icons drawn in code, so they are crisp at any size and take the theme's text color (no font symbols that may be missing)."""
import math

from .compat import QtCore, QtGui, enum

_ANTIALIAS = enum(QtGui.QPainter, "RenderHint", "Antialiasing")


def gear_icon(color, size=16, teeth=8):
    """A cog wheel in `color`, rendered at twice the size for sharp edges on any screen."""
    scale = 2
    pix = QtGui.QPixmap(size * scale, size * scale)
    pix.fill(QtGui.QColor(0, 0, 0, 0))
    p = QtGui.QPainter(pix)
    p.setRenderHint(_ANTIALIAS, True)
    half = size * scale / 2.0
    outer, inner = half * 0.94, half * 0.70
    path = QtGui.QPainterPath()
    step = 2 * math.pi / teeth
    for i in range(teeth):
        a = i * step
        for ang, radius in ((a - step * 0.30, inner), (a - step * 0.17, outer), (a + step * 0.17, outer), (a + step * 0.30, inner)):
            pt = QtCore.QPointF(half + radius * math.cos(ang), half + radius * math.sin(ang))
            path.moveTo(pt) if not path.elementCount() else path.lineTo(pt)
    path.closeSubpath()
    path.addEllipse(QtCore.QPointF(half, half), half * 0.30, half * 0.30)         # the hole
    path.setFillRule(enum(QtCore.Qt, "FillRule", "OddEvenFill"))
    p.setPen(enum(QtCore.Qt, "PenStyle", "NoPen"))
    p.setBrush(QtGui.QColor(color))
    p.drawPath(path)
    p.end()
    pix.setDevicePixelRatio(scale)
    return QtGui.QIcon(pix)
