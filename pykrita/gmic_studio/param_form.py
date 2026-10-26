"""Builds the parameter editor for a filter from its parsed definition."""
import html

from .compat import QtCore, QtGui, QtWidgets, enum, pyqtSignal
from .core import randomize as rnd

W = QtWidgets
_HORIZONTAL = enum(QtCore.Qt, "Orientation", "Horizontal")
_HLINE = enum(W.QFrame, "Shape", "HLine")
_SUNKEN = enum(W.QFrame, "Shadow", "Sunken")
_RICH = enum(QtCore.Qt, "TextFormat", "RichText")
_EXPANDING = enum(W.QSizePolicy, "Policy", "Expanding")
_SLIDER_STEPS = 1000
_LABEL_ROLE = enum(W.QFormLayout, "ItemRole", "LabelRole")
_FIELD_ROLE = enum(W.QFormLayout, "ItemRole", "FieldRole")
_SPAN_ROLE = enum(W.QFormLayout, "ItemRole", "SpanningRole")


def _slider_window(p):
    """Very wide ranges (seeds, sizes up to 1e6) would park the slider handle at the far left; let the slider
    cover a window around the default instead. The number box still accepts the whole range."""
    span = p.maximum - p.minimum
    if span <= 5000 or p.default is None:
        return None
    top = min(p.maximum, max(p.default * 2.5, p.minimum + 1000))
    return (p.minimum, max(top, p.default))


PAD = 8                    # left/right padding of the parameter rows, px


class SliderSpin(W.QWidget):
    """Slider + number box pair, like Krita's own slider-spin boxes."""
    valueChanged = pyqtSignal()
    scrubStarted = pyqtSignal()             # the slider handle was picked up ...
    scrubEnded = pyqtSignal()               # ... and let go

    def __init__(self, minimum, maximum, value, integer, slider_range=None):
        """`slider_range`: the part of the number range the slider covers (the number box may go beyond it)."""
        super().__init__()
        if integer:
            minimum, maximum = int(minimum), int(maximum)
        self._int, self._min, self._max = integer, minimum, maximum
        self._smin, self._smax = slider_range or (minimum, maximum)
        lay = W.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.slider = W.QSlider(_HORIZONTAL)
        self.slider.setMinimumWidth(90)
        self.slider.setRange(0, int(self._smax - self._smin) if integer else _SLIDER_STEPS)
        self.spin = W.QSpinBox() if integer else W.QDoubleSpinBox()
        self.spin.setRange(minimum, maximum)
        if not integer:
            self.spin.setDecimals(3)
            self.spin.setSingleStep(max((maximum - minimum) / 200.0, 0.001))
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        self.set_value(value)
        self.slider.valueChanged.connect(self._from_slider)
        self.slider.sliderPressed.connect(self.scrubStarted)
        self.slider.sliderReleased.connect(self.scrubEnded)
        self.spin.valueChanged.connect(self._from_spin)

    def _to_slider(self, v):
        v = max(self._smin, min(self._smax, v))
        return int(round(v - self._smin)) if self._int else int(round((v - self._smin) / ((self._smax - self._smin) or 1) * _SLIDER_STEPS))

    def _from_slider(self, pos):
        v = self._smin + (pos if self._int else pos / _SLIDER_STEPS * (self._smax - self._smin))
        self.spin.blockSignals(True)
        self.spin.setValue(int(round(v)) if self._int else v)
        self.spin.blockSignals(False)
        self.valueChanged.emit()

    def _from_spin(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(self._to_slider(v))
        self.slider.blockSignals(False)
        self.valueChanged.emit()

    def value(self):
        return self.spin.value()

    def set_value(self, v):
        for w in (self.slider, self.spin):
            w.blockSignals(True)
        self.spin.setValue(int(v) if self._int else float(v))
        self.slider.setValue(self._to_slider(self.spin.value()))
        for w in (self.slider, self.spin):
            w.blockSignals(False)


class PointEditor(W.QWidget):
    """A point on the image, as two percentage sliders (X, Y). The same point can be dragged on the preview."""
    valueChanged = pyqtSignal()

    def __init__(self, value):
        super().__init__()
        lay = W.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        self.x = SliderSpin(-100.0, 200.0, value[0], False, slider_range=(0.0, 100.0))
        self.y = SliderSpin(-100.0, 200.0, value[1], False, slider_range=(0.0, 100.0))
        for name, box in (("X", self.x), ("Y", self.y)):
            box.spin.setSuffix(" %")
            row = W.QHBoxLayout()
            row.setSpacing(3)
            row.addWidget(W.QLabel(name))
            row.addWidget(box, 1)
            lay.addLayout(row)
            box.valueChanged.connect(self.valueChanged)

    def value(self):
        return [self.x.value(), self.y.value()]

    def set_value(self, v):
        self.x.set_value(v[0])
        self.y.set_value(v[1])


class ColorButton(W.QPushButton):
    valueChanged = pyqtSignal()

    def __init__(self, value):
        super().__init__()
        self._value = list(value)
        self.clicked.connect(self._pick)
        self._paint()

    def _paint(self):
        r, g, b = self._value[:3]
        self.setText("#%02x%02x%02x" % (r, g, b) + (" / %d" % self._value[3] if len(self._value) > 3 else ""))
        self.setStyleSheet("QPushButton { background-color: rgb(%d,%d,%d); color: %s; }"
                           % (r, g, b, "#000" if (r * 299 + g * 587 + b * 114) / 1000 > 140 else "#fff"))

    def _pick(self):
        init = QtGui.QColor(*self._value)
        flag = enum(W.QColorDialog, "ColorDialogOption", "ShowAlphaChannel")
        col = W.QColorDialog.getColor(init, self, "Choose color", flag) if len(self._value) > 3 \
            else W.QColorDialog.getColor(init, self)
        if col.isValid():
            self._value = [col.red(), col.green(), col.blue()] + ([col.alpha()] if len(self._value) > 3 else [])
            self._paint()
            self.valueChanged.emit()

    def value(self):
        return list(self._value)

    def set_value(self, v):
        self._value = list(v)
        self._paint()


class PathEdit(W.QWidget):
    valueChanged = pyqtSignal()

    def __init__(self, value, folder):
        super().__init__()
        self._folder = folder
        lay = W.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = W.QLineEdit(value)
        btn = W.QPushButton("...")
        btn.setMaximumWidth(32)
        lay.addWidget(self.edit, 1)
        lay.addWidget(btn)
        btn.clicked.connect(self._browse)
        self.edit.editingFinished.connect(self.valueChanged)

    def _browse(self):
        path = (W.QFileDialog.getExistingDirectory(self, "Choose folder") if self._folder
                else W.QFileDialog.getOpenFileName(self, "Choose file")[0])
        if path:
            self.edit.setText(path)
            self.valueChanged.emit()

    def value(self):
        return self.edit.text()

    def set_value(self, v):
        self.edit.setText(str(v))


def _open_link(url):
    if url.lower().startswith(("http://", "https://")):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))


class LockLabel(W.QLabel):
    """A parameter's name. Double-click it (or right-click) to lock the value: Randomize then leaves it alone."""
    toggled = pyqtSignal(bool)

    def __init__(self, text, locked=False):
        super().__init__()
        self._base = text
        self._locked = False
        self.setToolTip("Double-click to lock this value; Randomize and Nudge will keep it.")
        self.set_locked(locked, notify=False)

    @property
    def locked(self):
        return self._locked

    def set_locked(self, locked, notify=True):
        self._locked = bool(locked)
        self.setText(("\U0001F512 " if self._locked else "") + self._base)
        font = self.font()
        font.setBold(self._locked)
        self.setFont(font)
        if notify:
            self.toggled.emit(self._locked)

    def mouseDoubleClickEvent(self, event):
        self.set_locked(not self._locked)

    def contextMenuEvent(self, event):
        menu = W.QMenu(self)
        menu.addAction("Unlock this value" if self._locked else "Lock this value (Randomize keeps it)").triggered.connect(
            lambda: self.set_locked(not self._locked))
        menu.exec(event.globalPos())


class ParamForm(W.QWidget):
    changed = pyqtSignal()
    locksChanged = pyqtSignal(int)            # how many values of the current filter are locked
    buttonPressed = pyqtSignal(int)           # a button parameter was pressed (its index)
    scrubFinished = pyqtSignal()              # a slider was let go (after changes made while it was held)

    def __init__(self):
        super().__init__()
        self._form = W.QFormLayout(self)
        self._form.setContentsMargins(PAD, 0, PAD, 0)          # room at both sides, so nothing touches the scroll bar or the edge
        self._form.setRowWrapPolicy(enum(W.QFormLayout, "RowWrapPolicy", "WrapAllRows"))   # label above: sliders get the full width
        self._fdef = None
        self._editors = {}   # param index -> (getter, setter)
        self._locks = {}     # filter id -> set of locked parameter indices (text)
        self._labels = {}    # param index -> LockLabel
        self._pressed = set()   # button parameters pressed for the next run only
        self._scrubbing = 0     # sliders being held right now
        self._silent = False    # setting values from a filter's report: no new preview
        self._hidden = {}       # hidden `value` parameters: they start at their default, a filter may report new ones
        self._shown = set()     # buttons behind the preview now on screen; Apply repeats them until a value changes
        self.changed.connect(lambda: self._shown.clear())

    @property
    def locked(self):
        return self._locks.setdefault(self._fdef.id, set()) if self._fdef is not None else set()

    def _label(self, i, p):
        if p.kind not in rnd.LOCKABLE:
            return p.label
        label = LockLabel(p.label, str(i) in self.locked)
        label.toggled.connect(lambda on, key=str(i): self._set_lock(key, on))
        self._labels[i] = label
        return label

    def _set_lock(self, key, on):
        (self.locked.add if on else self.locked.discard)(key)
        self.locksChanged.emit(len(self.locked))

    def points(self):
        """The point parameters as [{index, label, x, y, color}], for the handles drawn on the preview."""
        out = []
        if self._fdef is None:
            return out
        for i, p in enumerate(self._fdef.params):
            if p.kind == "point" and i in self._editors:
                x, y = self._editors[i][0]()
                out.append({"index": i, "label": p.label, "x": x, "y": y, "color": p.color or [255, 255, 255]})
        return out

    def set_point(self, index, x, y):
        """Move a point (what dragging its handle on the preview does)."""
        if index in self._editors and self._fdef.params[index].kind == "point":
            self._editors[index][1]([x, y])
            self.changed.emit()

    def clear_locks(self):
        self.locked.clear()
        for label in self._labels.values():
            label.set_locked(False, notify=False)
        self.locksChanged.emit(0)

    def randomize(self, strength=1.0, rng=None):
        """Give every unlocked parameter a random value (strength < 1: a nudge around the current ones)."""
        if self._fdef is None:
            return 0
        before = self.values()
        self.set_values(rnd.randomize(self._fdef, before, self.locked, rng, strength))
        return sum(1 for k, v in self.values().items() if before.get(k) != v)

    def clear(self):
        while self._form.rowCount():
            self._form.removeRow(0)
        self._editors = {}
        self._labels = {}
        self._pressed = set()
        self._shown = set()
        self._hidden = {}
        self._scrubbing = 0
        self._fdef = None

    def set_filter(self, fdef, values=None):
        self.clear()
        self._fdef = fdef
        for i, p in enumerate(fdef.params):
            before = self._form.rowCount()
            self._add_row(i, p)
            self._show_row(before, p)
        self.set_values(values if values is not None else fdef.defaults())

    def _show_row(self, row, p):
        """G'MIC's visibility suffix: `_0` hides the row (its value is still sent), `_1` grays it out."""
        if p.visibility == 2 or row >= self._form.rowCount():
            return
        for role in (_LABEL_ROLE, _FIELD_ROLE, _SPAN_ROLE):
            item = self._form.itemAt(row, role)
            if item is not None and item.widget() is not None:
                (item.widget().setVisible if p.visibility == 0 else item.widget().setEnabled)(False)

    def values(self):
        """The current values. Buttons are always 0 here, so presets and saved values never remember a press."""
        return {str(i): get() for i, (get, _set) in self._editors.items()}

    def run_values(self):
        """Values for one run: the same, plus 1 for each button pressed since the last run."""
        out = self.values()
        for i in self._pressed:
            out[str(i)] = 1
        return out

    def release_buttons(self):
        """A run started with the pressed buttons: they are spent, but what it produced still counts for Apply."""
        self._shown = set(self._pressed)
        self._pressed.clear()

    def apply_values(self):
        """Values for Apply: what the preview on screen was made from, including a button it was made with."""
        out = self.values()
        for i in self._pressed | self._shown:
            out[str(i)] = 1
        return out

    def set_values(self, values):
        if self._fdef is None:
            return
        for i, (_get, setter) in self._editors.items():
            p = self._fdef.params[i]
            setter(p.coerce(values.get(str(i), p.default)))
        self.changed.emit()

    def apply_status(self, status, sent):
        """Take over the values a filter reported (its status string, one `{...}` per value parameter).

        Only what the filter changed from what it was sent, and only where the form still holds what was sent
        (a newer edit wins). Nothing is run again: the picture already shows these values. True if anything moved."""
        if self._fdef is None:
            return False
        slots = [i for i, p in enumerate(self._fdef.params) if p.has_value]
        moved = False
        self._silent = True
        try:
            for i, text in zip(slots, engine_groups(status)):
                p = self._fdef.params[i]
                if text == "" or p.kind == "button" or i not in self._editors:
                    continue
                new, old = _from_report(p, text), p.coerce(sent.get(str(i), p.default))
                get, set_ = self._editors[i]
                if new is None or p.coerce(new) == old or get() != old:
                    continue
                set_(p.coerce(new))
                moved = True
        finally:
            self._silent = False
        return moved

    @property
    def scrubbing(self):
        """True while a slider handle is held: the preview should follow quickly instead of waiting."""
        return self._scrubbing > 0

    def _scrub_started(self):
        self._scrubbing += 1

    def _scrub_ended(self):
        self._scrubbing = max(0, self._scrubbing - 1)
        if not self._scrubbing:
            self.scrubFinished.emit()

    def _press(self, index):
        self._pressed.add(index)
        self.buttonPressed.emit(index)

    def reset(self):
        if self._fdef is not None:
            self.set_values(self._fdef.defaults())

    # -- row construction ------------------------------------------------
    def _register(self, i, widget, get, set_, signal):
        self._editors[i] = (get, set_)
        signal.connect(self._edited)
        return widget

    def _edited(self, *_):
        if not self._silent:
            self.changed.emit()

    def _add_row(self, i, p):
        k, f = p.kind, self._form
        if k == "separator":
            line = W.QFrame()
            line.setFrameShape(_HLINE)
            line.setFrameShadow(_SUNKEN)
            f.addRow(line)
        elif k == "note":
            lab = W.QLabel(p.default)
            lab.setWordWrap(True)
            lab.setTextFormat(_RICH)
            lab.setOpenExternalLinks(False)
            lab.linkActivated.connect(_open_link)
            f.addRow(lab)
        elif k == "link":
            url = p.extra or p.default
            lab = W.QLabel('<a href="%s">%s</a>' % (html.escape(url, quote=True), html.escape(p.default or url)))
            lab.setTextFormat(_RICH)
            lab.setOpenExternalLinks(False)
            lab.linkActivated.connect(_open_link)
            f.addRow(lab)
        elif k in ("int", "float"):
            w = SliderSpin(p.minimum, p.maximum, p.default, k == "int", slider_range=_slider_window(p))
            w.scrubStarted.connect(self._scrub_started)
            w.scrubEnded.connect(self._scrub_ended)
            f.addRow(self._label(i, p), self._register(i, w, w.value, w.set_value, w.valueChanged))
        elif k == "bool":
            w = W.QCheckBox()
            f.addRow(self._label(i, p), self._register(i, w, w.isChecked, w.setChecked, w.toggled))
        elif k == "choice":
            w = W.QComboBox()
            w.addItems(p.choices)
            f.addRow(self._label(i, p), self._register(i, w, w.currentIndex, w.setCurrentIndex, w.currentIndexChanged))
        elif k == "text" and p.multiline:
            w = W.QPlainTextEdit()
            w.setMaximumHeight(70)
            f.addRow(self._label(i, p), self._register(i, w, w.toPlainText, w.setPlainText, w.textChanged))
        elif k == "text":
            w = W.QLineEdit()
            f.addRow(self._label(i, p), self._register(i, w, w.text, w.setText, w.editingFinished))
        elif k == "color":
            w = ColorButton(p.default)
            f.addRow(self._label(i, p), self._register(i, w, w.value, w.set_value, w.valueChanged))
        elif k == "point":
            w = PointEditor(p.default)
            f.addRow(self._label(i, p), self._register(i, w, w.value, w.set_value, w.valueChanged))
        elif k in ("file", "folder"):
            w = PathEdit(p.default, k == "folder")
            f.addRow(self._label(i, p), self._register(i, w, w.value, w.set_value, w.valueChanged))
        elif k == "button":
            btn = W.QPushButton(p.label)
            btn.clicked.connect(lambda _checked=False, idx=i: self._press(idx))
            self._editors[i] = (lambda: 0, lambda _v: None)
            f.addRow(btn)
        else:                                    # value / hidden: nothing to edit
            self._hidden[i] = p.default
            self._editors[i] = (lambda i=i: self._hidden[i], lambda v, i=i: self._hidden.__setitem__(i, v))


def engine_groups(status):
    from .core import engine
    return engine.split_groups(status)


def _from_report(p, text):
    """A parameter value from the text of one reported group, or None when it cannot be read."""
    try:
        if p.kind == "int":
            return int(round(float(text)))
        if p.kind == "float":
            return float(text)
        if p.kind == "bool":
            return text.strip().lower() in ("1", "true")
        if p.kind == "choice":
            return int(float(text))
        if p.kind == "color":
            return [int(float(t)) for t in text.split(",")]
        if p.kind == "point":
            x, y = (float(t) for t in text.split(",")[:2])
            return [x, y]
    except ValueError:
        return None
    return text
