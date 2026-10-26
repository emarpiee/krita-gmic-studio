"""Everything that touches Krita's document API lives here, so the rest stays testable."""
import json
import zlib
from dataclasses import dataclass

from .compat import QtCore, qimage_to_bytes
from .core import pixels
from .core.engine import centered

SUPPORTED_DEPTHS = ("U8", "U16")


class BridgeError(Exception):
    """A problem the user can act on; the message is shown as-is."""


@dataclass
class Snapshot:
    doc: object
    node: object
    x: int
    y: int
    w: int
    h: int
    depth: int            # 8 or 16
    rgba: bytes           # native-endian RGBA at `depth`
    mask: bytes = None    # 8-bit selection coverage for the same rect, or None (no / full selection)
    selection: bool = False   # a selection limited the region or the mask


def _krita():
    from krita import Krita
    return Krita.instance()


def active_layer():
    app = _krita()
    doc = app.activeDocument()
    if doc is None:
        raise BridgeError("Open an image first.")
    node = doc.activeNode()
    if node is None:
        raise BridgeError("Select a paint layer first.")
    if node.type() != "paintlayer":
        raise BridgeError("G'MIC Studio works on paint layers. Select one (or merge this %s down)." % node.type())
    if node.locked():
        raise BridgeError("The layer is locked.")
    if node.colorModel() != "RGBA" or node.colorDepth() not in SUPPORTED_DEPTHS:
        raise BridgeError("Supported layers are RGB(A) 8-bit or 16-bit integer. This layer is %s %s; "
                          "convert it with Layer > Convert > Convert Layer Color Space."
                          % (node.colorModel(), node.colorDepth()))
    return doc, node


def _intersect(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None


def _view_center():
    """Center of what the user is looking at, in image coordinates, if Krita can tell us."""
    try:
        view = _krita().activeWindow().activeView()
        rect = view.visibleRect()
        return rect.x() + rect.width() // 2, rect.y() + rect.height() // 2
    except Exception:
        return None


def _region(doc, node, limit):
    """What the filter sees: the whole canvas (like Krita's own G'MIC), limited to the selection if any."""
    region = (0, 0, doc.width(), doc.height())
    sel = doc.selection()
    if sel is not None:
        region = _intersect(region, (sel.x(), sel.y(), sel.width(), sel.height()))
        if region is None:
            raise BridgeError("The selection is outside the canvas.")
    if region[2] <= 0 or region[3] <= 0:
        raise BridgeError("The canvas is empty.")
    if limit:
        x, y, w, h = region
        cw, ch = min(w, limit), min(h, limit)
        center = _view_center() or (x + w // 2, y + h // 2)
        cx = max(x, min(center[0] - cw // 2, x + w - cw))
        cy = max(y, min(center[1] - ch // 2, y + h - ch))
        region = (cx, cy, cw, ch)
    return region, sel


def region_size():
    """(w, h) of what a filter would process right now, without reading any pixels."""
    doc, node = active_layer()
    (_x, _y, w, h), _sel = _region(doc, node, None)
    return w, h


def grab(limit=None):
    """Read the active layer (or the part the filter will touch) as RGBA. `limit` = preview crop size."""
    doc, node = active_layer()
    (x, y, w, h), sel = _region(doc, node, limit)
    depth_name = node.colorDepth()
    raw = bytes(node.pixelData(x, y, w, h))
    sb = pixels.sample_bytes(depth_name)
    if len(raw) != w * h * 4 * sb:
        raise BridgeError("Krita returned an unexpected amount of pixel data.")
    mask = None
    if sel is not None:
        mask = bytes(sel.pixelData(x, y, w, h))
        if len(mask) != w * h:
            raise BridgeError("Krita returned an unexpected selection mask.")
        if mask.count(255) == len(mask):
            mask = None                               # everything selected: no blending needed
    return Snapshot(doc, node, x, y, w, h, 8 * sb, pixels.swap_rb(raw, sb), mask, sel is not None)


def to_preview_rgba8(snap):
    """8-bit RGBA copy of a snapshot, for previews and for the preview job."""
    return snap.rgba if snap.depth == 8 else pixels.u16_to_u8(snap.rgba)


def write(snap, rgba, mode, label, size=None, notes=None):
    """Put filtered RGBA into the document, undoably. `size` = (w, h) of the result if the filter resized it.

    Modes: "new_layer" (result above the original, which stays), "hide" (same, then the original is hidden)
    and "replace" (the original is removed once the result is in). Krita's Python `setPixelData` writes
    straight into the layer without an undo step, but adding/removing layers and the visibility action are
    undoable, so the result is built on a copy of the layer (same name, opacity, blend mode, masks) and
    added above the original. `notes` collects short messages for the user.
    """
    notes = notes if notes is not None else []
    node, doc = snap.node, snap.doc
    w, h = size or (snap.w, snap.h)
    if (w, h) != (snap.w, snap.h):
        dup = doc.createNode("%s (%s)" % (node.name(), label), "paintlayer")
        if (node.colorModel(), node.colorDepth()) != (doc.colorModel(), doc.colorDepth()):
            dup.setColorSpace(node.colorModel(), node.colorDepth(), node.colorProfile())
    else:
        if not hasattr(node, "duplicate"):
            raise BridgeError("This Krita version cannot duplicate layers from Python; please update Krita.")
        dup = node.duplicate()
        if dup is None:
            raise BridgeError("Krita could not duplicate the layer.")
    # A resized result (frame, border, crop...) is centered on the region it came from, like the filter saw it.
    x = snap.x - centered(w, snap.w) if (w, h) != (snap.w, snap.h) else snap.x
    y = snap.y - centered(h, snap.h) if (w, h) != (snap.w, snap.h) else snap.y
    dup.setPixelData(pixels.swap_rb(rgba, snap.depth // 8), x, y, w, h)
    dup.setName(node.name() if mode == "replace" else "%s (%s)" % (node.name(), label))
    dup.setVisible(True)
    if mode == "hide":
        _hide_original(doc, node, notes)         # first, while only the original is selected
    parent = node.parentNode() or doc.rootNode()
    parent.addChildNode(dup, node)
    if mode == "replace" and not _remove(node):
        _hide_original(doc, node, notes)         # Krita refused: never leave two visible copies without saying so
        notes.append("The original could not be removed, so it was hidden.")
    if not dup.visible():                        # never leave the result invisible
        dup.setVisible(True)
    doc.setActiveNode(dup)
    doc.refreshProjection()
    return dup


def _remove(node):
    """Remove a layer; True when it is really gone (Krita's remove() reports failure with False)."""
    try:
        return node.remove() is not False
    except Exception:
        return False


def _hide_original(doc, node, notes):
    """Hide `node` through Krita's own toggle action, which records an undo step."""
    if not node.visible():
        return
    done = False
    try:
        doc.setActiveNode(node)
        action = _krita().action("toggle_layer_visibility")
        if action is not None and doc.activeNode() is not None and doc.activeNode().uniqueId() == node.uniqueId():
            action.trigger()
            done = not node.visible()
    except Exception:
        done = False
    if done:
        notes.append("Original hidden (Ctrl+Z twice undoes both).")
    else:
        node.setVisible(False)                   # works, but Krita will not undo it
        notes.append("Original hidden, but this cannot be undone with Ctrl+Z.")


def signature():
    """Cheap fingerprint of what a preview would process (layer, selection, canvas, a tiny thumbnail).

    Krita tells plugins nothing when the user switches layers or paints, so the docker polls this and
    refreshes the preview when it changes. Returns None if there is nothing usable.
    """
    try:
        doc = _krita().activeDocument()
        node = doc.activeNode() if doc is not None else None
        if node is None:
            return None
        sel = doc.selection()
        thumb = node.thumbnail(48, 48)
        data = qimage_to_bytes(thumb) if thumb is not None and not thumb.isNull() else b""
        return (str(node.uniqueId().toString()), doc.width(), doc.height(),
                (sel.x(), sel.y(), sel.width(), sel.height()) if sel is not None else None, zlib.crc32(data))
    except Exception:
        return None


PRESET_ANNOTATION = "gmic_studio_presets"


def document_id():
    """Identity of the active document (stable while it is open and across saves), or None."""
    try:
        doc = _krita().activeDocument()
        return str(doc.rootNode().uniqueId().toString()) if doc is not None else None
    except Exception:
        return None


def document_presets_io():
    """(read, write) functions for the presets stored inside the *currently active* document.

    They stay bound to that document even if the user switches to another one later. Krita keeps the data in
    an image annotation, which is saved in the .kra file. `setAnnotation` does not mark the document as
    changed by itself, so `write` does, otherwise Krita might close without offering to save.
    """
    doc = _krita().activeDocument()
    if doc is None:
        raise BridgeError("Open an image first.")

    def read():
        raw = bytes(doc.annotation(PRESET_ANNOTATION))
        return json.loads(raw.decode("utf-8")) if raw else {}

    def write(data):
        if data.get("presets"):
            raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
            doc.setAnnotation(PRESET_ANNOTATION, "G'MIC Studio presets", QtCore.QByteArray(raw))
        elif PRESET_ANNOTATION in doc.annotationTypes():
            doc.removeAnnotation(PRESET_ANNOTATION)               # keep files clean when nothing is left
        doc.setModified(True)

    return read, write
