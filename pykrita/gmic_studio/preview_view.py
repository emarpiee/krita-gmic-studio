"""Zoomable, pannable preview: mouse wheel zooms at the cursor, drag pans, double-click toggles fit / 100%."""
from .compat import QtCore, QtGui, QtWidgets, enum, pyqtSignal

_LEFT = enum(QtCore.Qt, "MouseButton", "LeftButton")
_OPEN = enum(QtCore.Qt, "CursorShape", "OpenHandCursor")
_CLOSED = enum(QtCore.Qt, "CursorShape", "ClosedHandCursor")
_SMOOTH = enum(QtGui.QPainter, "RenderHint", "SmoothPixmapTransform")
MIN_ZOOM, MAX_ZOOM = 0.02, 32.0
_TILE = 8


def _checker():
    """Neutral two-tone checkerboard so transparent areas are visible in any theme."""
    pix = QtGui.QPixmap(_TILE * 2, _TILE * 2)
    pix.fill(QtGui.QColor(92, 92, 92))
    p = QtGui.QPainter(pix)
    p.fillRect(0, 0, _TILE, _TILE, QtGui.QColor(120, 120, 120))
    p.fillRect(_TILE, _TILE, _TILE, _TILE, QtGui.QColor(120, 120, 120))
    p.end()
    return pix


# G'MIC-Qt's preview modes (the number is what its gui_split_preview command takes)
MODES = [("Full", 0), ("Forward Horizontal", 1), ("Forward Vertical", 2), ("Backward Horizontal", 3),
         ("Backward Vertical", 4), ("Duplicate Top", 5), ("Duplicate Left", 6), ("Duplicate Bottom", 7),
         ("Duplicate Right", 8), ("Duplicate Horizontal", 9), ("Duplicate Vertical", 10), ("Checkered", 11),
         ("Checkered Inverse", 12)]
DUPLICATE_MODES = range(5, 11)


def original_regions(mode, sx, sy):
    """Parts of the image (as fractions x0, y0, x1, y1) that show the original in a split mode."""
    fx, fy = sx / 100.0, sy / 100.0
    return {1: [(0, 0, 1, fy)], 2: [(0, 0, fx, 1)], 3: [(0, fy, 1, 1)], 4: [(fx, 0, 1, 1)],
            11: [(0, 0, fx, fy), (fx, fy, 1, 1)], 12: [(fx, 0, 1, fy), (0, fy, fx, 1)]}.get(mode, [])


HANDLE_RADIUS = 6
HANDLE_HIT = 10                  # px around a handle's centre that grabs it
_SIZE_ALL = enum(QtCore.Qt, "CursorShape", "SizeAllCursor")
_POINTING = enum(QtCore.Qt, "CursorShape", "PointingHandCursor")


class PreviewView(QtWidgets.QWidget):
    zoomChanged = pyqtSignal(float)
    splitMoved = pyqtSignal(float, float)             # split point, % of the image
    splitReleased = pyqtSignal()
    handleMoved = pyqtSignal(int, float, float)       # parameter index, x %, y % (of the image)
    dragStarted = pyqtSignal()                        # a handle was picked up ...
    dragFinished = pyqtSignal()                       # ... and let go

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(200)
        self.setSizePolicy(enum(QtWidgets.QSizePolicy, "Policy", "Expanding"),
                           enum(QtWidgets.QSizePolicy, "Policy", "Expanding"))
        self.setCursor(_OPEN)
        self.setFocusPolicy(enum(QtCore.Qt, "FocusPolicy", "ClickFocus"))      # a click here leaves the search box
        self.setToolTip("Mouse wheel: zoom   Drag: pan   Double-click: fit / 100%")
        self._image = None
        self._zoom = 1.0
        self._off = QtCore.QPointF(0, 0)
        self._drag = None
        self._checker = _checker()
        self._handles = []          # draggable points: dicts index, x, y (percent of the image), color [r, g, b]
        self._grab = None           # the handle being dragged
        self._overlays = []
        self._frame = None          # (w, h) of the original when the result is bigger/smaller: drawn centered
        self._before = None         # the original, drawn over the result in the split modes
        self._mode, self._split = 0, [50.0, 50.0]
        self._split_grab = False
        self.setMouseTracking(True)
        self._busy = False
        self._phase = 0.0
        self._ticker = QtCore.QTimer(self)
        self._ticker.setInterval(40)
        self._ticker.timeout.connect(self._advance)

    # -- public ---------------------------------------------------------
    @property
    def zoom(self):
        return self._zoom

    def set_handles(self, handles):
        """Show draggable handles for points given as percentages of the image (ignored while one is being dragged)."""
        if self._grab is None:
            self._handles = [dict(h) for h in handles]
            self.update()

    def add_overlay(self, widget, corner):
        """Keep a small control (a child widget) in a corner of the preview."""
        self._overlays.append((widget, corner))
        self.place_overlays()

    def place_overlays(self):
        left, right = 6, self.width() - 6
        for widget, corner in self._overlays:
            if widget.isHidden():
                continue
            widget.adjustSize()
            if corner.endswith("left"):
                widget.move(left, 6)
                left += widget.width() + 4
            else:
                right -= widget.width()
                widget.move(right, 6)
                right -= 4
            widget.raise_()

    def set_compare(self, before, mode=0, split=(50.0, 50.0)):
        """Show `before` (the original) beside the result in a split mode; `before=None` shows only the result."""
        self._before, self._mode, self._split = (before, mode, list(split)) if mode in (1, 2, 3, 4, 11, 12) else (None, 0, list(split))
        self.update()

    def set_frame(self, size):
        """Outline the part of the result that lies over the original (what a canvas of that size would show)."""
        self._frame = tuple(size) if size else None
        self.update()

    def _draw_frame(self, p, target):
        if self._frame is None or self._image is None:
            return
        from .core.engine import centered
        fw, fh = self._frame
        iw, ih = self._image.width(), self._image.height()
        z = self._zoom
        box = QtCore.QRectF(target.x() + centered(iw, fw) * z, target.y() + centered(ih, fh) * z, fw * z, fh * z)
        p.save()
        p.setPen(enum(QtCore.Qt, "PenStyle", "NoPen"))
        p.setBrush(QtGui.QColor(0, 0, 0, 110))                      # dim what the original's canvas would not show
        for part in (QtCore.QRectF(target.left(), target.top(), target.width(), box.top() - target.top()),
                     QtCore.QRectF(target.left(), box.bottom(), target.width(), target.bottom() - box.bottom()),
                     QtCore.QRectF(target.left(), box.top(), box.left() - target.left(), box.height()),
                     QtCore.QRectF(box.right(), box.top(), target.right() - box.right(), box.height())):
            if part.width() > 0 and part.height() > 0:
                p.drawRect(part)
        p.setBrush(enum(QtCore.Qt, "BrushStyle", "NoBrush"))
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 220), 1, enum(QtCore.Qt, "PenStyle", "DashLine")))
        p.drawRect(box)
        p.restore()

    @property
    def comparing(self):
        return self._before is not None

    def _split_pos(self):
        return QtCore.QPointF(self._off.x() + self._zoom * self._split[0] / 100.0 * self._image.width(),
                              self._off.y() + self._zoom * self._split[1] / 100.0 * self._image.height())

    def _handle_pos(self, h):
        return QtCore.QPointF(self._off.x() + self._zoom * h["x"] / 100.0 * self._image.width(),
                              self._off.y() + self._zoom * h["y"] / 100.0 * self._image.height())

    def _handle_at(self, pos):
        if self._image is None:
            return None
        for h in reversed(self._handles):                 # the one drawn on top wins
            c = self._handle_pos(h)
            if (c.x() - pos.x()) ** 2 + (c.y() - pos.y()) ** 2 <= HANDLE_HIT ** 2:
                return h
        return None

    @property
    def dragging(self):
        return self._grab is not None

    @property
    def busy(self):
        return self._busy

    def set_busy(self, busy):
        """Show or hide a thin moving bar along the bottom edge (it takes no layout space)."""
        busy = bool(busy)
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self._ticker.start()
        else:
            self._ticker.stop()
        self.update()

    def _advance(self):
        self._phase = (self._phase + 0.03) % 1.0
        self.update()

    def show_image(self, image):
        """Show `image`; keep the current zoom/pan if it has the same size as the previous one."""
        old = self._image
        keep = old is not None and image is not None and image.size() == old.size()
        if (not keep and old is not None and image is not None and not old.isNull() and not image.isNull()
                and abs(old.width() * image.height() - image.width() * old.height()) <= 0.01 * old.width() * image.height()):
            self._zoom *= old.width() / float(image.width())        # same picture at another resolution: keep the view
            keep = True
            self._emit()
        self._image = image
        if not keep:
            self.fit()
        self.update()

    def fit(self):
        if self._image is None or self._image.isNull():
            return
        iw, ih = self._image.width(), self._image.height()
        self._zoom = max(MIN_ZOOM, min(self.width() / float(iw), self.height() / float(ih)))
        self._center()
        self._emit()

    def actual_size(self, anchor=None):
        self.zoom_to(1.0, anchor)

    def zoom_by(self, factor, anchor=None):
        self.zoom_to(self._zoom * factor, anchor)

    def zoom_to(self, zoom, anchor=None):
        """Set the zoom, keeping the image point under `anchor` (widget coords; default = centre) fixed."""
        if self._image is None:
            return
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        a = anchor if anchor is not None else QtCore.QPointF(self.width() / 2.0, self.height() / 2.0)
        img_pt = (a - self._off) / self._zoom            # image coordinate under the anchor
        self._zoom = zoom
        self._off = a - img_pt * zoom
        self._emit()
        self.update()

    # -- internals ------------------------------------------------------
    def _center(self):
        iw, ih = self._image.width() * self._zoom, self._image.height() * self._zoom
        self._off = QtCore.QPointF((self.width() - iw) / 2.0, (self.height() - ih) / 2.0)

    def _emit(self):
        self.zoomChanged.emit(self._zoom)

    @staticmethod
    def _pos(event):
        p = event.position() if hasattr(event, "position") else event.pos()
        return QtCore.QPointF(p.x(), p.y())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.place_overlays()
        if self._image is not None and self._fits_all():
            self.fit()

    def _fits_all(self):
        """True while the image is still shown fitted (so a resize should refit)."""
        iw, ih = self._image.width(), self._image.height()
        return abs(self._zoom - min(self.width() / float(iw), self.height() / float(ih))) < 0.02 or self._zoom <= MIN_ZOOM

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120.0
        if steps:
            self.zoom_by(1.2 ** steps, self._pos(event))
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == _LEFT:
            if self._before is not None and self._image is not None:
                c, pos = self._split_pos(), self._pos(event)
                if (c.x() - pos.x()) ** 2 + (c.y() - pos.y()) ** 2 <= (HANDLE_HIT + 4) ** 2:
                    self._split_grab = True
                    self.setCursor(_SIZE_ALL)
                    return
            h = self._handle_at(self._pos(event))
            if h is not None:
                self._grab = h
                self.setCursor(_SIZE_ALL)
                self.dragStarted.emit()
                return
            self._drag = (self._pos(event), QtCore.QPointF(self._off))
            self.setCursor(_CLOSED)

    def mouseMoveEvent(self, event):
        if self._split_grab and self._image is not None:
            pos = self._pos(event)
            pct = lambda v, off, size: max(0.0, min(100.0, (v - off) / self._zoom / size * 100.0))
            self._split = [pct(pos.x(), self._off.x(), self._image.width()), pct(pos.y(), self._off.y(), self._image.height())]
            self.update()
            self.splitMoved.emit(*self._split)
            return
        if self._grab is not None and self._image is not None:
            pos = self._pos(event)
            pct = lambda v, off, size: max(0.0, min(100.0, (v - off) / self._zoom / size * 100.0))
            self._grab["x"] = pct(pos.x(), self._off.x(), self._image.width())
            self._grab["y"] = pct(pos.y(), self._off.y(), self._image.height())
            self.update()
            self.handleMoved.emit(int(self._grab["index"]), self._grab["x"], self._grab["y"])
        elif self._drag is not None:
            start, off = self._drag
            self._off = off + (self._pos(event) - start)
            self.update()
        else:
            self.setCursor(_POINTING if self._handle_at(self._pos(event)) is not None else _OPEN)

    def mouseReleaseEvent(self, event):
        was_dragging = self._grab is not None
        if self._split_grab:
            self._split_grab = False
            self.setCursor(_OPEN)
            self.splitReleased.emit()
            return
        self._grab = None
        self._drag = None
        self.setCursor(_OPEN)
        if was_dragging:
            self.dragFinished.emit()

    def mouseDoubleClickEvent(self, event):
        if self._handle_at(self._pos(event)) is not None:
            return                                          # double-clicking a handle must not change the zoom
        if abs(self._zoom - 1.0) < 1e-6:
            self.fit()
            self.update()
        else:
            self.actual_size(self._pos(event))

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), self.palette().window())
        if self._image is None or self._image.isNull():
            p.setPen(self.palette().windowText().color())
            p.drawText(self.rect(), enum(QtCore.Qt, "AlignmentFlag", "AlignCenter"), "No preview yet")
            self._draw_busy(p)                          # the very first preview needs the indicator most
            p.end()
            return
        target = QtCore.QRectF(self._off.x(), self._off.y(),
                               self._image.width() * self._zoom, self._image.height() * self._zoom)
        p.save()
        p.setClipRect(target.intersected(QtCore.QRectF(self.rect())))
        p.drawTiledPixmap(self.rect(), self._checker)
        p.restore()
        p.setRenderHint(_SMOOTH, self._zoom < 2.0)         # crisp pixels when zoomed in
        p.drawImage(target, self._image)
        self._draw_compare(p, target)
        self._draw_frame(p, target)
        self._draw_handles(p)
        self._draw_zoom(p)
        self._draw_busy(p)
        p.end()

    def _draw_compare(self, p, target):
        if self._before is None:
            return
        p.save()
        for x0, y0, x1, y1 in original_regions(self._mode, *self._split):
            part = QtCore.QRectF(target.x() + x0 * target.width(), target.y() + y0 * target.height(),
                                 (x1 - x0) * target.width(), (y1 - y0) * target.height())
            p.setClipRect(part)
            p.drawImage(target, self._before)
        p.restore()
        c = self._split_pos()
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 230), 1, enum(QtCore.Qt, "PenStyle", "DashLine")))
        if self._mode in (1, 3):
            p.drawLine(QtCore.QPointF(target.left(), c.y()), QtCore.QPointF(target.right(), c.y()))
        elif self._mode in (2, 4):
            p.drawLine(QtCore.QPointF(c.x(), target.top()), QtCore.QPointF(c.x(), target.bottom()))
        else:
            p.drawLine(QtCore.QPointF(target.left(), c.y()), QtCore.QPointF(target.right(), c.y()))
            p.drawLine(QtCore.QPointF(c.x(), target.top()), QtCore.QPointF(c.x(), target.bottom()))
        p.setRenderHint(enum(QtGui.QPainter, "RenderHint", "Antialiasing"), True)
        p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 200), 3))
        p.setBrush(QtGui.QColor(255, 255, 255))
        p.drawEllipse(c, 5, 5)
        p.setRenderHint(enum(QtGui.QPainter, "RenderHint", "Antialiasing"), False)

    def _draw_handles(self, p):
        """A ring in each point's own color, with a dark edge so it shows on any picture."""
        p.setRenderHint(enum(QtGui.QPainter, "RenderHint", "Antialiasing"), True)
        for h in self._handles:
            c = self._handle_pos(h)
            r = HANDLE_RADIUS + (2 if h is self._grab else 0)
            p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 200), 4))
            p.setBrush(enum(QtCore.Qt, "BrushStyle", "NoBrush"))
            p.drawEllipse(c, r, r)
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 235), 2))
            p.setBrush(QtGui.QColor(*h["color"]))
            p.drawEllipse(c, r, r)
        p.setRenderHint(enum(QtGui.QPainter, "RenderHint", "Antialiasing"), False)

    def _draw_busy(self, p):
        if not self._busy:
            return
        h, w = 3, self.width()
        seg = max(30, w // 4)
        x = int((w + seg) * self._phase) - seg
        p.setPen(enum(QtCore.Qt, "PenStyle", "NoPen"))
        p.setBrush(QtGui.QColor(0, 0, 0, 90))
        p.drawRect(0, self.height() - h, w, h)
        p.setBrush(self.palette().highlight())
        p.drawRect(max(0, x), self.height() - h, min(w, x + seg) - max(0, x), h)

    def _draw_zoom(self, p):
        """A small '67%' badge in the corner, so the panel needs no separate readout."""
        text = "%d%%" % round(self._zoom * 100)
        fm = p.fontMetrics()
        w, h = fm.horizontalAdvance(text) + 12, fm.height() + 4
        box = QtCore.QRectF(self.width() - w - 6, self.height() - h - 6, w, h)
        p.setRenderHint(_SMOOTH, False)
        p.setPen(enum(QtCore.Qt, "PenStyle", "NoPen"))
        p.setBrush(QtGui.QColor(0, 0, 0, 150))
        p.drawRoundedRect(box, 4, 4)
        p.setPen(QtGui.QColor(255, 255, 255, 235))
        p.drawText(box, enum(QtCore.Qt, "AlignmentFlag", "AlignCenter"), text)
