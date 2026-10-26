"""Minimal stand-in for Krita's Python API, enough to exercise the plugin headlessly."""
try:
    from PyQt6 import QtGui, QtWidgets
except ImportError:
    from PyQt5 import QtGui, QtWidgets


class _Uid:
    def __init__(self, v): self.v = v
    def __eq__(self, other): return isinstance(other, _Uid) and other.v == self.v
    def __hash__(self): return hash(self.v)
    def toString(self): return "{%d}" % self.v


class Rect:
    def __init__(self, x, y, w, h):
        self._r = (x, y, w, h)
    def x(self): return self._r[0]
    def y(self): return self._r[1]
    def width(self): return self._r[2]
    def height(self): return self._r[3]


class Selection(Rect):
    def __init__(self, x, y, w, h, value=255):
        super().__init__(x, y, w, h)
        self.value = value
    def pixelData(self, x, y, w, h):
        out = bytearray(w * h)
        for j in range(h):
            for i in range(w):
                if self.x() <= x + i < self.x() + self.width() and self.y() <= y + j < self.y() + self.height():
                    out[j * w + i] = self.value
        return bytes(out)


class Node:
    def __init__(self, name, w, h, depth="U8", model="RGBA", kind="paintlayer", fill=None):
        self._name, self._kind, self._model, self._depth = name, kind, model, depth
        self._w, self._h = w, h
        bps = 2 if depth == "U16" else 1
        self.pixels = bytearray(fill if fill is not None else bytes(range(256)) * (w * h * 4 * bps // 256 + 1))[:w * h * 4 * bps]
        self.children, self._parent, self.set_calls = [], None, []
        self._visible = True
    def type(self): return self._kind
    def locked(self): return False
    def colorModel(self): return self._model
    def colorDepth(self): return self._depth
    def bounds(self): return Rect(0, 0, self._w, self._h)
    def name(self): return self._name
    def setName(self, n): self._name = n
    def colorProfile(self): return "sRGB"
    def setColorSpace(self, model, depth, profile): self._model, self._depth = model, depth
    def parentNode(self): return self._parent
    def addChildNode(self, child, above):
        child._parent = self
        self.children.insert(self.children.index(above) + 1 if above in self.children else len(self.children), child)
    def remove(self):
        self._parent.children.remove(self)
        return True
    def duplicate(self):
        n = Node(self._name, self._w, self._h, self._depth, self._model, fill=bytes(self.pixels))
        n._visible = self._visible
        return n
    def visible(self): return self._visible
    def setVisible(self, v): self._visible = v; self.direct_visibility_writes = getattr(self, "direct_visibility_writes", 0) + 1
    def uniqueId(self): return _Uid(id(self))
    def thumbnail(self, w, h):
        import zlib
        img = QtGui.QImage(w, h, QtGui.QImage.Format.Format_RGBA8888 if hasattr(QtGui.QImage, "Format") else QtGui.QImage.Format_RGBA8888)
        crc = zlib.crc32(bytes(self.pixels))
        img.fill(QtGui.QColor(crc & 255, (crc >> 8) & 255, (crc >> 16) & 255))
        return img
    def pixelData(self, x, y, w, h):
        bps = 2 if self._depth == "U16" else 1
        out = bytearray()
        for j in range(y, y + h):
            out += self.pixels[(j * self._w + x) * 4 * bps:(j * self._w + x + w) * 4 * bps]
        return bytes(out)
    def setPixelData(self, data, x, y, w, h):
        bps = 2 if self._depth == "U16" else 1
        self.set_calls.append((x, y, w, h, len(data)))
        if x < 0 or y < 0 or x + w > self._w or y + h > self._h:
            return                                   # real Krita layers are unbounded; the stub's are not
        for j in range(h):
            row = data[j * w * 4 * bps:(j + 1) * w * 4 * bps]
            start = ((y + j) * self._w + x) * 4 * bps
            self.pixels[start:start + len(row)] = row


class Document:
    def __init__(self, node, selection=None):
        self.node, self._sel, self.refreshed = node, selection, 0
        self._annotations, self._modified = {}, False
        self.root = Node("root", 0, 0, kind="grouplayer")
        node._parent = self.root
        self.root.children.append(node)
    def width(self): return self.node._w
    def height(self): return self.node._h
    def annotation(self, t): return self._annotations.get(t, b"")
    def setAnnotation(self, key, description, data): self._annotations[key] = bytes(data)
    def annotationTypes(self): return list(self._annotations)
    def removeAnnotation(self, t): self._annotations.pop(t, None)
    def setModified(self, v): self._modified = bool(v)
    def modified(self): return self._modified
    def activeNode(self): return self.node
    def setActiveNode(self, n): self.node = n
    def selection(self): return self._sel
    def rootNode(self): return self.root
    def refreshProjection(self): self.refreshed += 1
    def colorModel(self): return "RGBA"
    def colorDepth(self): return "U8"
    def createNode(self, name, kind): return Node(name, self.node._w, self.node._h)


class _Action:
    def __init__(self, krita): self.krita = krita
    def trigger(self):                       # Krita's undoable toggle acts on the active layer
        if self.krita.toggle_works:
            node = self.krita.doc.node
            node._visible = not node._visible
            self.krita.undoable_toggles += 1


class _Krita:
    doc = None
    extensions, factories = [], []
    toggle_works, undoable_toggles = True, 0
    def action(self, name): return _Action(self) if name == "toggle_layer_visibility" else None
    def activeDocument(self): return self.doc
    def activeWindow(self): return None
    def addExtension(self, e): self.extensions.append(e)
    def addDockWidgetFactory(self, f): self.factories.append(f)


_instance = _Krita()


class Krita:
    @staticmethod
    def instance(): return _instance


class Extension:
    def __init__(self, parent=None): pass


class DockWidget(QtWidgets.QDockWidget):
    pass


class DockWidgetFactoryBase:
    DockRight = 2


class DockWidgetFactory:
    def __init__(self, ident, area, cls):
        self.id, self.area, self.cls = ident, area, cls
