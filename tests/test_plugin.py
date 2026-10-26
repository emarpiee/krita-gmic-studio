"""End-to-end through the Qt layer with a stub Krita and a fake gmic (copies in.png to out.png)."""
import os
import stat
import sys
import time

import pytest

pytest.importorskip("PyQt6.QtWidgets") if os.environ.get("GMIC_TEST_QT") != "5" else pytest.importorskip("PyQt5.QtWidgets")

import krita
import gmic_studio.core.engine as engine_mod
from gmic_studio.compat import QtWidgets
from gmic_studio.core import filters
from gmic_studio import krita_bridge as bridge

HERE = os.path.dirname(os.path.abspath(__file__))
FILTERS, FAVORITES, PRESETS = 0, 1, 2      # tab positions in the docker


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def studio(app, tmp_path, monkeypatch):
    monkeypatch.setenv("GMIC_STUDIO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.delenv("GMIC_PATH", raising=False)
    fake = tmp_path / "gmic"
    fake.write_text("#!%s\n" % sys.executable + open(os.path.join(HERE, "fake_gmic.py")).read().split("\n", 1)[1])
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    import gmic_studio.studio as st
    from gmic_studio.core import log as log_mod
    log_mod.reset()
    if st._instance is not None:                       # never drop a Studio whose worker threads are still running
        st._instance.wait_for_threads()
        app.processEvents()
    st._instance = None
    s = st.get_studio()
    s.settings.set("gmic_path", str(fake))
    import gmic_studio.docker as dock
    created, original_init = [], dock.StudioDocker.__init__

    def tracking_init(self, *a, **k):
        original_init(self, *a, **k)
        created.append(self)

    monkeypatch.setattr(dock.StudioDocker, "__init__", tracking_init)
    yield s
    for d in created:                                  # a docker left over from this test must not keep reacting in the next
        d.watch.stop()
        d.timer.stop()
        d._pv_timer.stop()


def wait(app, cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


def ready(app, s):
    s.refresh()
    wait(app, lambda: s.exe and len(s.index.filters) > 0)
    assert s.exe_version == "3.9.9"


def use_doc(node, selection=None):
    krita.Krita.instance().doc = krita.Document(node, selection)
    return krita.Krita.instance().doc


@pytest.mark.parametrize("depth", ["U8", "U16"])
@pytest.mark.parametrize("mode", ["new_layer", "replace"])
def test_apply_is_undoable_and_lossless(app, studio, depth, mode):
    """Krita records layer add/remove but not setPixelData, so the original layer is never written to."""
    ready(app, studio)
    studio.settings.set("result_mode", mode)
    node = krita.Node("L", 6, 4, depth)
    before = bytes(node.pixels)
    doc = use_doc(node)
    fdef = studio.index.get("Basics > Blur")
    msgs = []
    studio.status.connect(lambda m, e: msgs.append((m, e)))
    studio.apply(fdef, fdef.defaults())
    wait(app, lambda: not studio.busy and doc.node is not node)
    assert [m for m, e in msgs if e] == []
    result = doc.node
    assert bytes(result.pixels) == before            # fake gmic copies the input: identity pipeline
    assert node.set_calls == [] and bytes(node.pixels) == before
    assert result.set_calls == [(0, 0, 6, 4, len(before))] and doc.refreshed == 1
    if mode == "replace":
        assert doc.root.children == [result] and result.name() == "L"
    else:
        assert doc.root.children == [node, result] and result.name() == "L (Blur)"
    assert studio.settings.get("last_filter") == "Basics > Blur"


def test_default_mode_keeps_original(app, studio):
    assert studio.settings.get("result_mode") == "new_layer"


def test_selection_region_and_full_selection_ignored(app, studio):
    ready(app, studio)
    node = krita.Node("L", 8, 8)
    doc = use_doc(node, krita.Selection(2, 2, 3, 3))
    snap = bridge.grab()
    assert (snap.x, snap.y, snap.w, snap.h) == (2, 2, 3, 3) and snap.selection
    assert snap.mask is None                          # fully selected region needs no blending
    doc = use_doc(node, krita.Selection(2, 2, 3, 3, value=128))
    assert bridge.grab().mask == b"\x80" * 9
    fdef = studio.index.get("Basics > Sepia")
    studio.apply(fdef, {})
    wait(app, lambda: not studio.busy and doc.node is not node)
    assert doc.node.set_calls == [(2, 2, 3, 3, 3 * 3 * 4)] and node.set_calls == []


def test_preview_signal(app, studio):
    ready(app, studio)
    use_doc(krita.Node("L", 10, 10))
    got = []
    studio.previewReady.connect(lambda b, a: got.append((b.width(), a.width())))
    studio.request_preview(studio.index.get("Basics > Blur"), {})
    wait(app, lambda: got)
    assert got == [(10, 10)]


def test_stale_preview_is_dropped(app, studio):
    ready(app, studio)
    use_doc(krita.Node("L", 10, 10))
    got = []
    studio.previewReady.connect(lambda b, a: got.append(1))
    f = studio.index.get("Basics > Blur")
    studio.request_preview(f, {})
    studio.request_preview(f, {})
    wait(app, lambda: got)
    time.sleep(0.5)
    app.processEvents()
    assert got == [1]


@pytest.mark.parametrize("kw,msg", [
    (dict(model="CMYKA"), "RGB"),
    (dict(depth="F32"), "RGB"),
    (dict(kind="vectorlayer"), "paint layers"),
])
def test_unsupported_layers_explain_themselves(app, studio, kw, msg):
    use_doc(krita.Node("L", 4, 4, **kw))
    with pytest.raises(bridge.BridgeError, match=msg):
        bridge.grab()


def test_no_document(app, studio):
    krita.Krita.instance().doc = None
    with pytest.raises(bridge.BridgeError, match="Open an image"):
        bridge.grab()


def test_selection_outside_layer(app, studio):
    use_doc(krita.Node("L", 4, 4), krita.Selection(50, 50, 3, 3))
    with pytest.raises(bridge.BridgeError, match="outside the canvas"):
        bridge.grab()


def test_preview_crop_is_limited_and_centered(app, studio):
    use_doc(krita.Node("L", 100, 80))
    snap = bridge.grab(limit=32)
    assert (snap.w, snap.h) == (32, 32) and (snap.x, snap.y) == (34, 24)


def test_docker_builds_and_selects(app, studio):
    ready(app, studio)
    use_doc(krita.Node("L", 12, 12))
    import gmic_studio
    from gmic_studio.docker import StudioDocker
    d = StudioDocker()
    d.show()
    d.search.setText("blur")
    app.processEvents()
    item = d.tree.invisibleRootItem().child(0)
    d.tree.setCurrentItem(item)
    assert d.fdef is not None and d.fdef.command in ("blur", "blur_xy")
    d.form.set_values({"0": 5})
    d.timer.stop()
    d._run_preview()
    wait(app, lambda: d._after is not None)
    d._toggle_favorite()
    assert studio.settings.is_favorite(d.fdef.id)
    d._apply()
    wait(app, lambda: not studio.busy and "Applied" in d.status.text())
    assert krita.Krita.instance().doc.node.name() == "L (%s)" % d.fdef.name
    d.search.setText("")
    app.processEvents()
    assert d.tree.topLevelItemCount() >= 1           # the categories (favorites and presets have their own tabs)


def test_param_form_all_kinds(app):
    from gmic_studio.core import filters
    from gmic_studio.param_form import ParamForm
    text = '''#@gui T > All : cmd
#@gui : a = int(3,0,10)
#@gui : b = float(1.5,0,5)
#@gui : c = bool(1)
#@gui : d = choice(2,"x","y","z")
#@gui : e = text("hi")
#@gui : f = color(1,2,3)
#@gui : g = point(10,20)
#@gui : h = file("")
#@gui : i = folder("")
#@gui : j = note("n")
#@gui : k = link("L","https://example.org")
#@gui : l = separator()
#@gui : m = button(0)
#@gui : n = text(1,"multi")
'''
    fdef = filters.parse_definitions(text)[0]
    form = ParamForm()
    got = []
    form.changed.connect(lambda: got.append(1))
    form.set_filter(fdef)
    vals = form.values()
    assert vals["0"] == 3 and abs(vals["1"] - 1.5) < 1e-6 and vals["2"] is True and vals["3"] == 2
    assert vals["4"] == "hi" and vals["5"] == [1, 2, 3] and vals["6"] == [10.0, 20.0]
    form.set_values({"0": 7, "3": 0})
    assert form.values()["0"] == 7 and form.values()["3"] == 0
    cmd, frags = fdef.command_line(form.values())
    assert frags[0] == "7" and frags[3] == "0"
    form.reset()
    assert form.values()["0"] == 3


def test_settings_dialog_saves(app, studio):
    from gmic_studio.settings_dialog import SettingsDialog
    dlg = SettingsDialog(studio)
    dlg.size.setValue(256)
    dlg.output.setCurrentIndex(dlg.output.findData("replace"))
    dlg._accept()
    assert studio.settings.get("preview_size") == 256 and studio.settings.get("result_mode") == "replace"
    wait(app, lambda: studio.exe)


def test_plugin_package_registers(app, studio):
    import importlib
    import gmic_studio
    importlib.reload(gmic_studio)
    k = krita.Krita.instance()
    assert k.factories[-1].id == "gmic_studio_docker" and k.extensions


def test_region_is_whole_canvas_even_if_layer_is_small(app, studio):
    """Like Krita's own G'MIC: tiles/gradients span the canvas, not just the painted part of the layer."""
    node = krita.Node("L", 40, 30)
    node.bounds = lambda: krita.Rect(10, 10, 5, 5)     # painted area is tiny
    use_doc(node)
    snap = bridge.grab()
    assert (snap.x, snap.y, snap.w, snap.h) == (0, 0, 40, 30)


def test_a_resized_result_still_replaces_the_original_when_asked(app, studio):
    node = krita.Node("L", 10, 10)
    doc = use_doc(node)
    snap = bridge.grab()
    out = bridge.write(snap, bytes(20 * 15 * 4), "replace", "Frame", size=(20, 15))
    assert doc.root.children == [out] and out.name() == "L"                    # the original is gone, the name is kept
    assert out.set_calls == [(-5, -2, 20, 15, 20 * 15 * 4)] and node.set_calls == []      # centered on the 10x10 original
    node = krita.Node("M", 10, 10)
    doc = use_doc(node)
    out = bridge.write(bridge.grab(), bytes(20 * 15 * 4), "new_layer", "Frame", size=(20, 15))
    assert doc.root.children == [node, out] and out.name() == "M (Frame)"


def test_replace_that_krita_refuses_hides_the_original_and_says_so(app, studio, monkeypatch):
    node = krita.Node("L", 10, 10)
    doc = use_doc(node)
    node.remove = lambda: False
    notes = []
    out = bridge.write(bridge.grab(), bytes(10 * 10 * 4), "replace", "Blur", notes=notes)
    assert node in doc.root.children and out in doc.root.children and not node.visible()
    assert any("could not be removed" in n for n in notes)


def _preview_sizes(app, studio, fdef):
    got, msgs = [], []
    studio.previewReady.connect(lambda b, a: got.append((b.width(), b.height(), a.width(), a.height())))
    studio.status.connect(lambda m, e: msgs.append(m))
    studio.request_preview(fdef, fdef.defaults())
    wait(app, lambda: got)
    return got[-1], [m for m in msgs if m.startswith("Preview ready")][-1]


def test_preview_is_whole_image_when_small(app, studio):
    ready(app, studio)
    use_doc(krita.Node("L", 120, 90))
    size, msg = _preview_sizes(app, studio, studio.index.get("Basics > Blur"))
    assert size == (120, 90, 120, 90) and "whole image" in msg


def test_big_image_follows_filter_preview_zoom(app, studio):
    ready(app, studio)
    studio.settings.set("preview_full_mp", 0.001)          # pretend 120x90 is "big"
    studio.settings.set("preview_size", 32)
    use_doc(krita.Node("L", 120, 90), krita.Selection(0, 0, 60, 90, value=128))
    f = studio.index.get("Basics > Blur")
    f.preview_factor = 0.0                                  # filter needs actual size -> 1:1 crop
    size, msg = _preview_sizes(app, studio, f)
    assert size == (32, 32, 32, 32) and "1:1 crop" in msg
    f.preview_factor = 1.0                                  # filter made for whole-image thumbnails
    studio.settings.set("preview_full_mp", 0.001)
    import gmic_studio.studio as st_mod
    old, st_mod.THUMB_SIDE = st_mod.THUMB_SIDE, 60
    try:
        size, msg = _preview_sizes(app, studio, f)
    finally:
        st_mod.THUMB_SIDE = old
    assert size == (40, 60, 40, 60) and "scaled image" in msg and "selection" in msg


def test_preview_and_apply_share_one_seed_until_rerolled(app, studio):
    ready(app, studio)
    seen = []
    real = engine_mod.Runner

    class Spy(real):
        def __init__(self, exe, job, timeout=600):
            seen.append(job.seed)
            super().__init__(exe, job, timeout)

    engine_mod.Runner, orig = Spy, engine_mod.Runner
    try:
        node = krita.Node("L", 8, 8)
        doc = use_doc(node)
        f = studio.index.get("Basics > Blur")
        studio.request_preview(f, f.defaults())
        wait(app, lambda: len(seen) == 1)
        studio.apply(f, f.defaults())
        wait(app, lambda: not studio.busy and doc.node is not node)
        assert seen[0] == seen[1] == studio.seed
        old = studio.seed
        assert studio.new_seed() != old
    finally:
        engine_mod.Runner = orig


def test_preview_view_zoom_and_pan(app):
    from gmic_studio.compat import QtCore, QtGui
    from gmic_studio.preview_view import PreviewView
    view = PreviewView()
    view.resize(200, 150)
    img = QtGui.QImage(400, 300, QtGui.QImage.Format.Format_RGBA8888 if hasattr(QtGui.QImage, "Format")
                       else QtGui.QImage.Format_RGBA8888)
    img.fill(0)
    zooms = []
    view.zoomChanged.connect(zooms.append)
    view.show_image(img)
    assert abs(view.zoom - 0.5) < 1e-6                                    # 400x300 fitted into 200x150
    anchor = QtCore.QPointF(60, 90)
    before = (anchor - view._off) / view.zoom                              # image point under the cursor
    view.zoom_by(2.0, anchor)
    assert abs(view.zoom - 1.0) < 1e-6 and zooms[-1] == view.zoom
    after = (anchor - view._off) / view.zoom
    assert abs(before.x() - after.x()) < 1e-6 and abs(before.y() - after.y()) < 1e-6   # it did not move
    view.zoom_to(1000)                                                     # clamped
    assert view.zoom == 32.0
    view.actual_size()
    assert view.zoom == 1.0
    view.fit()
    assert abs(view.zoom - 0.5) < 1e-6
    same = QtGui.QImage(img)
    view.zoom_by(3.0)
    z = view.zoom
    view.show_image(same)                                                  # same size: keep the zoom (compare)
    assert view.zoom == z


def test_rounding_by_a_pixel_is_not_a_resize(app, studio, tmp_path):
    """Filters that round to even sizes must not turn into a 'new size' layer."""
    from gmic_studio.core import pngio
    node = krita.Node("L", 5, 3)
    use_doc(node)
    snap = bridge.grab()
    out = tmp_path / "out.png"
    pngio.write(str(out), 6, 4, bytes(range(6 * 4 * 4)), pngio.RGBA, 8)          # one pixel bigger each way
    data, size = studio._decode_result((6, 4, 8, str(out), str(tmp_path)), snap)
    assert size == (5, 3) and len(data) == 5 * 3 * 4
    pngio.write(str(out), 20, 15, bytes(20 * 15 * 4), pngio.RGBA, 8)             # a real resize
    data, size = studio._decode_result((20, 15, 8, str(out), str(tmp_path)), snap)
    assert size == (20, 15)


def test_studio_passes_probes_only_when_enabled(app, studio):
    ready(app, studio)
    node = krita.Node("L", 40, 40, fill=bytes(40 * 40 * 4))
    for x in range(15, 25):                                     # a little opaque content, rest transparent
        for y in range(15, 25):
            node.pixels[(y * 40 + x) * 4 + 3] = 255
    use_doc(node)
    jobs = []
    real = engine_mod.Runner

    class Spy(real):
        def __init__(self, exe, job, timeout=600):
            jobs.append(job)
            super().__init__(exe, job, timeout)

    engine_mod.Runner = Spy
    try:
        f = studio.index.get("Basics > Blur")
        studio.request_preview(f, f.defaults())                     # off by default: no cleanup requested
        wait(app, lambda: len(jobs) == 1)
        assert not jobs[0].bg_probes
        wait(app, lambda: not studio._threads)
        studio.settings.set("transparent_bg", True)
        studio.request_preview(f, f.defaults())
        wait(app, lambda: len(jobs) == 2)
        assert jobs[1].bg_probes and len(jobs[1].bg_probes) >= 3
        wait(app, lambda: not studio._threads)
        studio.settings.set("transparent_bg", False)
        studio.request_preview(f, f.defaults())
        wait(app, lambda: len(jobs) == 3)
        assert not jobs[2].bg_probes
    finally:
        engine_mod.Runner = real


def test_docker_checkbox_controls_the_setting(app, studio):
    ready(app, studio)
    use_doc(krita.Node("L", 12, 12))
    from gmic_studio.docker import StudioDocker
    d = StudioDocker()
    assert not d.tbg.isChecked() and d.tbg.text() == "Force transparent background"       # off on a fresh install
    d.tbg.setChecked(True)
    assert studio.settings.get("transparent_bg") is True
    d.tbg.setChecked(False)
    assert studio.settings.get("transparent_bg") is False


# -- apply modes ---------------------------------------------------------------------------
@pytest.fixture
def toggle():
    k = krita.Krita.instance()
    k.toggle_works, k.undoable_toggles = True, 0
    yield k
    k.toggle_works, k.undoable_toggles = True, 0


def _apply_and_wait(app, studio, doc, node):
    f = studio.index.get("Basics > Blur")
    studio.apply(f, f.defaults())
    wait(app, lambda: not studio.busy and doc.node is not node)
    return doc.node


def test_hide_mode_uses_the_undoable_toggle(app, studio, toggle):
    ready(app, studio)
    studio.settings.set("result_mode", "hide")
    node = krita.Node("L", 6, 4)
    doc = use_doc(node)
    msgs = []
    studio.status.connect(lambda m, e: msgs.append(m))
    result = _apply_and_wait(app, studio, doc, node)
    assert doc.root.children == [node, result] and result.name() == "L (Blur)"
    assert node.visible() is False and result.visible() is True
    assert toggle.undoable_toggles == 1 and not hasattr(node, "direct_visibility_writes")
    assert node.set_calls == [] and "Original hidden (Ctrl+Z twice undoes both)." in msgs[-1]


def test_hide_mode_falls_back_when_the_toggle_does_not_work(app, studio, toggle):
    ready(app, studio)
    studio.settings.set("result_mode", "hide")
    toggle.toggle_works = False
    node = krita.Node("L", 6, 4)
    doc = use_doc(node)
    msgs = []
    studio.status.connect(lambda m, e: msgs.append(m))
    result = _apply_and_wait(app, studio, doc, node)
    assert node.visible() is False and result.visible() is True
    assert node.direct_visibility_writes == 1 and "cannot be undone" in msgs[-1]


def test_hide_mode_leaves_an_already_hidden_original_alone(app, studio, toggle):
    ready(app, studio)
    studio.settings.set("result_mode", "hide")
    node = krita.Node("L", 6, 4)
    node._visible = False
    doc = use_doc(node)
    result = _apply_and_wait(app, studio, doc, node)
    assert toggle.undoable_toggles == 0 and node.visible() is False and result.visible() is True


def test_hide_mode_with_a_resized_result(app, studio, toggle):
    node = krita.Node("L", 10, 10)
    doc = use_doc(node)
    snap = bridge.grab()
    out = bridge.write(snap, bytes(20 * 15 * 4), "hide", "Frame", size=(20, 15))
    assert doc.root.children == [node, out] and node.visible() is False and out.visible() is True


def test_new_layer_mode_keeps_the_original_visible(app, studio, toggle):
    ready(app, studio)
    studio.settings.set("result_mode", "new_layer")
    node = krita.Node("L", 6, 4)
    doc = use_doc(node)
    result = _apply_and_wait(app, studio, doc, node)
    assert node.visible() is True and toggle.undoable_toggles == 0 and result in doc.root.children


def test_docker_apply_mode_combo_is_saved_and_used(app, studio, toggle):
    ready(app, studio)
    from gmic_studio.docker import StudioDocker
    d = StudioDocker()
    assert [d.mode.itemData(i) for i in range(d.mode.count())] == ["new_layer", "hide", "replace"]
    d.mode.setCurrentIndex(d.mode.findData("hide"))
    assert studio.settings.get("result_mode") == "hide"
    node = krita.Node("L", 6, 4)
    doc = use_doc(node)
    d.search.setText("sepia")
    d.tree.setCurrentItem(d.tree.invisibleRootItem().child(0))
    d._apply()
    wait(app, lambda: not studio.busy and doc.node is not node)
    assert node.visible() is False


# -- docker layout and the preset library --------------------------------------------------
def _stub_save_dialog(monkeypatch, name, lock_seed, scope="library"):
    import gmic_studio.docker as dock
    scopes = ["library", "document"] if scope == "both" else [scope]
    monkeypatch.setattr(dock.PresetSaveDialog, "scopes", lambda self: scopes)
    monkeypatch.setattr(dock.PresetSaveDialog, "exec", lambda self: 1)
    monkeypatch.setattr(dock.PresetSaveDialog, "name", lambda self: name)
    monkeypatch.setattr(dock.PresetSaveDialog, "lock_seed", lambda self: lock_seed)


def _list_names(d):
    return [d.plist.item(i).text() for i in range(d.plist.count())]


def _make_docker(app, studio, doc=True):
    ready(app, studio)
    if doc:
        use_doc(krita.Node("L", 12, 12))
    from gmic_studio.docker import StudioDocker
    return StudioDocker()


def test_original_layout_preview_is_below_the_lists(app, studio):
    from gmic_studio.compat import QtCore
    d = _make_docker(app, studio, doc=False)
    d.resize(360, 900)
    d.show()
    for _ in range(5):
        app.processEvents()
    y = lambda w: w.mapTo(d.widget(), QtCore.QPoint(0, 0)).y()
    assert not d.banner.isVisibleTo(d.widget())                          # gmic is fine: no status row at all
    assert y(d.search) < y(d.tabs) < y(d.form) < y(d.save_btn) < y(d.live) < y(d.preview) < y(d.status) < y(d.apply_btn)


def test_docker_stays_narrow(app, studio):
    """The docker's minimum width must stay close to the original layout (no wide rows)."""
    d = _make_docker(app, studio, doc=False)
    assert d.widget().minimumSizeHint().width() <= 340, d.widget().minimumSizeHint().width()


def test_every_preset_of_every_filter_is_in_one_list(app, studio, monkeypatch):
    d = _make_docker(app, studio)
    blur, sepia, sharpen = (studio.index.get("Basics > " + n) for n in ("Blur", "Sepia", "Sharpen"))
    studio.save_preset("Dreamy", blur, {"0": 9.0}, lock_seed=False)
    studio.save_preset("Warm", sepia, {})
    studio.save_preset("Crisp", sharpen, {"0": 150.0})
    d._show_filter(sepia, None)                                    # a filter with a single preset is selected
    assert len(_list_names(d)) == 3                                # ...yet all presets are listed
    assert d.tabs.tabText(PRESETS) == "Presets (3)"
    d.only_this.setChecked(True)                                   # opt-in narrowing
    assert _list_names(d) == ["Warm  ·  Sepia"]
    d.only_this.setChecked(False)
    assert sorted(_list_names(d)) == ["Crisp  ·  Sharpen", "Dreamy  ·  Blur", "Warm  ·  Sepia"]


def test_clicking_a_preset_switches_filter_and_restores_values_and_seed(app, studio):
    d = _make_docker(app, studio)
    blur = studio.index.get("Basics > Blur")
    studio.seed = 4242
    p = studio.save_preset("Dreamy", blur, {"0": 7.5}, lock_seed=True)
    d._show_filter(studio.index.get("Basics > Sepia"), None)
    studio.seed = 1
    d.tabs.setCurrentIndex(PRESETS)
    d.plist.setCurrentRow(0)
    assert d.fdef.id == blur.id and d.form.values()["0"] == 7.5
    assert studio.seed == 4242 and d.preset_id == p["id"] and d.update_btn.isEnabled()
    d.tabs.setCurrentIndex(0)
    d.search.setText("")
    d.tree.setCurrentItem(d.tree.invisibleRootItem().child(0).child(0) if d.tree.invisibleRootItem().child(0).childCount() else d.tree.invisibleRootItem().child(0))
    assert d.preset_id is None and not d.update_btn.isEnabled()      # choosing a plain filter leaves preset mode


def test_search_finds_presets_and_switches_tab_when_needed(app, studio):
    d = _make_docker(app, studio)
    studio.save_preset("Zzyzx glow", studio.index.get("Basics > Blur"), {})
    d.tabs.setCurrentIndex(0)
    d.search.setText("zzyzx")                                      # no filter matches, one preset does
    assert d.tabs.currentIndex() == PRESETS and _list_names(d) == ["Zzyzx glow  ·  Blur"]
    d.search.setText("sepia")                                      # filters match, no preset does
    assert d.tabs.currentIndex() == 0 and d.plist.count() == 0
    d.search.setText("blur")                                       # both match: the tab stays where it is
    assert d.tabs.currentIndex() == 0 and d.plist.count() == 1


def test_double_clicking_a_preset_applies_it_and_moves_it_to_the_top(app, studio):
    d = _make_docker(app, studio)
    node = krita.Node("L", 6, 4)
    doc = use_doc(node)
    blur, sepia = studio.index.get("Basics > Blur"), studio.index.get("Basics > Sepia")
    studio.save_preset("Alpha", blur, {"0": 2.0})
    studio.save_preset("Beta", sepia, {})
    assert _list_names(d)[0].startswith("Alpha")
    d.tabs.setCurrentIndex(PRESETS)
    d.plist.setCurrentRow(1)                                       # Beta
    d.plist.itemDoubleClicked.emit(d.plist.item(1))
    wait(app, lambda: not studio.busy and doc.node is not node)
    assert _list_names(d)[0].startswith("Beta")                    # most recently used first
    assert d.plist.currentItem().text().startswith("Beta")         # selection followed it


def test_save_preset_stores_values_and_seed_and_asks_before_replacing(app, studio, monkeypatch):
    d = _make_docker(app, studio)
    blur = studio.index.get("Basics > Blur")
    d._show_filter(blur, None)
    d.form.set_values({"0": 7.5})
    studio.seed = 4242
    _stub_save_dialog(monkeypatch, "Strong blur", True)
    d._save_preset()
    p = studio.presets.find(blur.id, "Strong blur")
    assert p["values"]["0"] == 7.5 and p["seed"] == 4242 and d.preset_id == p["id"]
    assert _list_names(d) == ["Strong blur  ·  Blur"]
    d.form.set_values({"0": 9.0})
    _stub_save_dialog(monkeypatch, "strong blur", False)
    monkeypatch.setattr(d, "_confirm", lambda text: False)
    d._save_preset()
    assert studio.presets.find(blur.id, "Strong blur")["values"]["0"] == 7.5
    monkeypatch.setattr(d, "_confirm", lambda text: True)
    d._save_preset()
    assert studio.presets.find(blur.id, "Strong blur")["values"]["0"] == 9.0 and len(studio.presets) == 1


def test_update_rename_delete_and_unavailable_filters(app, studio, monkeypatch):
    d = _make_docker(app, studio)
    blur = studio.index.get("Basics > Blur")
    p = studio.save_preset("One", blur, {"0": 2.0}, lock_seed=True)
    studio.save_preset("Ghost", type(blur)((), "Gone", "fx_gone", []), {})
    d.tabs.setCurrentIndex(PRESETS)
    d.plist.setCurrentRow([i for i in range(d.plist.count()) if d.plist.item(i).text().startswith("One")][0])
    d.form.set_values({"0": 11.0})
    studio.seed = 99
    d.update_btn.click()
    got = studio.presets.get(p["id"])
    assert got["values"]["0"] == 11.0 and got["seed"] == 99                      # seed kept (and refreshed)
    assert studio.rename_preset(p["id"], "Renamed") and "Renamed" in "".join(_list_names(d))
    ghost = [d.plist.item(i) for i in range(d.plist.count()) if "Ghost" in d.plist.item(i).text()][0]
    assert not ghost.flags() & _enabled_flag() and "filter not available" in ghost.text()
    monkeypatch.setattr(d, "_confirm", lambda text: True)
    d._delete_preset(p["id"])
    assert studio.presets.get(p["id"]) is None and d.preset_id is None and not d.update_btn.isEnabled()


def _enabled_flag():
    from gmic_studio.compat import QtCore, enum
    return enum(QtCore.Qt, "ItemFlag", "ItemIsEnabled")


def test_changing_the_presets_file_copies_existing_presets(app, studio, monkeypatch, tmp_path):
    ready(app, studio)
    from gmic_studio.settings_dialog import SettingsDialog
    studio.save_preset("Keep me", studio.index.get("Basics > Blur"), {"0": 3.0})
    dlg = SettingsDialog(studio)
    target = tmp_path / "synced" / "presets.json"
    dlg.presets_path.setText(str(target))
    from gmic_studio.compat import QtWidgets, enum
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(lambda *a, **k: enum(QtWidgets.QMessageBox, "StandardButton", "Yes")))
    dlg._accept()
    assert studio.presets.path == str(target) and target.exists()
    assert studio.presets.find("Basics > Blur", "Keep me")["values"]["0"] == 3.0
    dlg2 = SettingsDialog(studio)
    dlg2.presets_path.setText("")                                            # back to the default location
    dlg2._accept()
    assert "synced" not in studio.presets.path and studio.presets.path.endswith("presets.json")
    assert studio.presets.find("Basics > Blur", "Keep me")                    # still readable from the default file


# -- robustness: nothing may fail silently ---------------------------------------------------
def test_unexpected_errors_in_preview_and_apply_are_reported_and_logged(app, studio, monkeypatch):
    ready(app, studio)
    use_doc(krita.Node("L", 8, 8))
    f = studio.index.get("Basics > Blur")
    msgs, busy = [], []
    studio.status.connect(lambda m, e: msgs.append((m, e)))
    studio.previewBusy.connect(busy.append)
    real = bridge.region_size
    monkeypatch.setattr(bridge, "region_size", lambda: (_ for _ in ()).throw(RuntimeError("Krita went away")))
    studio.request_preview(f, f.defaults())
    assert msgs[-1][1] is True and "unexpectedly" in msgs[-1][0] and "Krita went away" in msgs[-1][0]
    assert busy[-1] is False
    monkeypatch.setattr(bridge, "region_size", real)
    monkeypatch.setattr(bridge, "grab", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    studio.apply(f, f.defaults())
    assert "Apply failed unexpectedly" in msgs[-1][0] and studio.busy is False         # not stuck busy
    from gmic_studio.core import log as log_mod
    text = open(log_mod.path()).read()
    assert "Krita went away" in text and "Traceback" in text and "boom" in text
    monkeypatch.undo()
    node = krita.Node("L", 8, 8)
    doc = use_doc(node)
    assert _apply_and_wait(app, studio, doc, node) is not node                      # and it still works afterwards


def test_gmic_does_not_inherit_stdin_and_jobs_are_logged(app, studio, tmp_path, monkeypatch):
    ready(app, studio)
    seen = {}
    real = engine_mod.subprocess.Popen

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(engine_mod.subprocess, "Popen", spy)
    engine_mod.Runner(studio.exe, engine_mod.Job("blur", ["2"], 2, 1, bytes(8), 8)).run()
    assert seen["stdin"] == engine_mod.subprocess.DEVNULL
    from gmic_studio.core import log as log_mod
    assert "run blur 2 2x1" in open(log_mod.path()).read()


def test_hide_mode_never_leaves_the_result_invisible(app, studio, toggle):
    node = krita.Node("L", 10, 10)
    doc = use_doc(node)
    real_add = doc.root.addChildNode

    def add_hidden(child, above):                       # as if Krita's toggle had hidden the new layer too
        real_add(child, above)
        child._visible = False

    doc.root.addChildNode = add_hidden
    out = bridge.write(bridge.grab(), bytes(10 * 10 * 4), "hide", "X")
    assert out.visible() is True and node.visible() is False


def test_signature_follows_layer_pixels_and_selection(app, studio):
    node = krita.Node("L", 20, 20)
    doc = use_doc(node)
    a = bridge.signature()
    assert a == bridge.signature() and a is not None                                   # stable while nothing changes
    node.pixels[0:4] = b"\x01\x02\x03\x04"
    b = bridge.signature()
    assert b != a                                                                      # painting changes it
    doc._sel = krita.Selection(1, 1, 5, 5)
    assert bridge.signature() != b                                                     # selection changes it
    other = krita.Node("M", 20, 20)
    doc.node = other
    assert bridge.signature() != b
    krita.Krita.instance().doc = None
    assert bridge.signature() is None


def test_preview_refreshes_by_itself_when_the_layer_changes(app, studio):
    studio.settings.set("preview_delay_ms", 10)
    d = _make_docker(app, studio, doc=False)
    node = krita.Node("L", 12, 12)
    doc = use_doc(node)
    d.show()
    d.search.setText("blur")
    d.tabs.setCurrentIndex(0)
    d._select_in_tree("Basics > Blur")
    d._on_tree_item(d.tree.currentItem(), None)
    count = []
    studio.previewReady.connect(lambda b, a: count.append(1))
    wait(app, lambda: len(count) == 1)
    d._watch_tick()                                      # baseline
    n = len(count)
    d._watch_tick()
    app.processEvents()
    time.sleep(0.2)
    app.processEvents()
    assert len(count) == n                               # nothing changed: no refresh
    node.pixels[0:4] = b"\x09\x09\x09\xff"               # the user painted something
    d._watch_tick()
    wait(app, lambda: len(count) == n + 1)
    doc.node = krita.Node("Other", 12, 12)               # the user picked another layer
    d._watch_tick()
    wait(app, lambda: len(count) == n + 2)
    studio.settings.set("watch_layer", False)
    doc.node.pixels[0:4] = b"\x07\x07\x07\xff"
    d._watch_tick()
    time.sleep(0.2)
    app.processEvents()
    assert len(count) == n + 2                           # the option turns it off


def test_busy_indicator_shows_on_the_preview_while_a_preview_renders(app, studio):
    d = _make_docker(app, studio)
    d.show()
    studio.previewBusy.emit(True)
    assert d.preview.busy
    assert d.preview._ticker.isActive()
    studio.previewBusy.emit(False)
    assert not d.preview.busy and not d.preview._ticker.isActive()


def test_apply_still_works_if_the_presets_file_is_unwritable(app, studio, monkeypatch):
    d = _make_docker(app, studio)
    node = krita.Node("L", 6, 4)
    doc = use_doc(node)
    blur = studio.index.get("Basics > Blur")
    studio.save_preset("P", blur, {"0": 2.0})
    d.tabs.setCurrentIndex(PRESETS)
    d.plist.setCurrentRow(0)
    monkeypatch.setattr(type(studio.presets), "touch", lambda self, pid: (_ for _ in ()).throw(OSError("read-only")))
    d._apply()
    wait(app, lambda: not studio.busy and doc.node is not node)


# -- resized results, background removal on them, and a stable filter list ---------------------
def test_resized_result_is_centered_on_the_original(app, studio):
    node = krita.Node("L", 100, 60)
    doc = use_doc(node, krita.Selection(10, 20, 40, 20, value=255))
    snap = bridge.grab()
    assert (snap.x, snap.y, snap.w, snap.h) == (10, 20, 40, 20)
    out = bridge.write(snap, bytes(60 * 30 * 4), "new_layer", "Frame", size=(60, 30))      # +20 px, +10 px
    assert out.set_calls == [(0, 15, 60, 30, 60 * 30 * 4)]                                    # 10-(20//2), 20-(10//2)
    smaller = bridge.write(snap, bytes(30 * 10 * 4), "new_layer", "Crop", size=(30, 10))     # -10 px, -10 px
    assert smaller.set_calls == [(15, 25, 30, 10, 30 * 10 * 4)]                               # floor(-10/2) = -5


_NEEDS_GMIC = pytest.mark.skipif(__import__("shutil").which("gmic") is None, reason="needs a real gmic")
_FILTERS = """
fx_t_grow : resize 160,120,1,4,0,0,0.5,0.5 fill "c==3?255:i"
fx_t_noisy : fill "c==3?255:(i==0?((x*7+y*13)%20):i)"
fx_t_dots : fill "c==3?255:(((x%6)<3&&(y%6)<3)?i:0)"
fx_t_gradient : fill "c==3?255:(i==0?(x*255/w):i)"
fx_t_wash : fill "c==3?(i<255?128:255):i"
fx_t_blur : blur 2
"""


def _bg_run(tmp_path, command, bg=True, size=(120, 80), pupil=False):
    """Run a test filter from the definitions above through the real pipeline; returns (image, pixel function)."""
    import shutil
    from gmic_studio.compat import QtGui
    from gmic_studio.core import pixels
    fmt = QtGui.QImage.Format.Format_RGBA8888 if hasattr(QtGui.QImage, "Format") else QtGui.QImage.Format_RGBA8888
    defs = tmp_path / "bg.gmic"
    defs.write_text(_FILTERS)
    W, H = size
    img = QtGui.QImage(W, H, fmt)
    img.fill(0)
    p = QtGui.QPainter(img)
    p.setBrush(QtGui.QColor(30, 160, 60))
    p.setPen(QtGui.QPen(QtGui.QColor(30, 160, 60)))
    p.drawEllipse(30, 15, 60, 50)
    if pupil:
        p.setBrush(QtGui.QColor(0, 0, 0))
        p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0)))
        p.drawEllipse(52, 30, 12, 12)                                   # real black content of the artwork
    p.end()
    data = bytes(img.constBits().asstring(img.sizeInBytes()))
    probes = pixels.background_probes(data, W, H, 4, 3)
    job = engine_mod.Job(command, [], W, H, data, 8, None, "auto", bg_probes=probes if bg else None, definitions=str(defs))
    w, h, d, path, wd = engine_mod.Runner(engine_mod.locate(), job).run()
    out = QtGui.QImage(path).convertToFormat(fmt)
    shutil.rmtree(wd, ignore_errors=True)

    def px(x, y):
        c = out.pixelColor(x, y)
        return (round(c.redF() * 255), round(c.greenF() * 255), round(c.blueF() * 255), round(c.alphaF() * 255))

    return out, px


@_NEEDS_GMIC
def test_a_flat_fill_becomes_transparent_everywhere_including_what_the_filter_added(app, tmp_path):
    out, px = _bg_run(tmp_path, "fx_t_grow", bg=False)
    assert (out.width(), out.height()) == (160, 120) and px(2, 2) == (0, 0, 0, 255) and px(30, 30) == (0, 0, 0, 255)
    out, px = _bg_run(tmp_path, "fx_t_grow")
    assert px(2, 2)[3] == 0 and px(20 + 5, 20 + 5)[3] == 0                     # the border and the original's empty area
    assert px(20 + 60, 20 + 40) == (30, 160, 60, 255)                         # the artwork is untouched


@_NEEDS_GMIC
def test_a_noisy_fill_is_cleared_too(app, tmp_path):
    out, px = _bg_run(tmp_path, "fx_t_noisy", bg=False)
    fill = {px(x, 3)[:3] for x in range(3, 12)}
    assert len(fill) > 3 and all(px(x, 3)[3] == 255 and max(px(x, 3)[:3]) < 20 for x in range(3, 12))   # opaque, dark, not flat
    out, px = _bg_run(tmp_path, "fx_t_noisy")
    assert all(px(x, y)[3] == 0 for x in (3, 4, 5, 6, 7, 100, 110) for y in (3, 4, 70, 75))      # every noisy pixel cleared
    assert px(60, 40) == (30, 160, 60, 255)


@_NEEDS_GMIC
def test_content_the_filter_left_alone_stays_but_what_it_blacked_out_goes(app, tmp_path):
    """Dots on black: black the filter *produced* is background; the artwork's own black pupil is kept."""
    out, px = _bg_run(tmp_path, "fx_t_dots", pupil=True)
    assert px(3, 3)[3] == 0                                                   # empty canvas
    inside_gaps = [px(x, y) for x in range(40, 50) for y in range(25, 30) if (x % 6) >= 3 or (y % 6) >= 3]
    assert inside_gaps and all(c[3] == 0 for c in inside_gaps)                # green blacked out by the filter -> cleared
    dots = [px(x, y) for x in range(40, 50) for y in range(25, 30) if (x % 6) < 3 and (y % 6) < 3]
    assert dots and all(c[3] == 255 and c[1] > 100 for c in dots)             # the dots keep their color, fully opaque
    assert px(58, 36) == (0, 0, 0, 255)                                       # the pupil: black before, black after -> kept


@_NEEDS_GMIC
def test_fills_that_are_not_a_flat_background_are_left_alone(app, tmp_path):
    out, px = _bg_run(tmp_path, "fx_t_gradient")                              # a gradient is content, not a background
    assert px(3, 3)[3] == 255 and px(110, 3)[3] == 255
    out, px = _bg_run(tmp_path, "fx_t_wash")                                  # semi-transparent wash: not an opaque fill
    assert 0 < px(3, 3)[3] < 255
    out, px = _bg_run(tmp_path, "fx_t_blur")                                  # nothing was filled: nothing to remove
    assert px(3, 3)[3] == 0 and px(60, 40)[3] == 255


def test_filter_list_keeps_open_groups_scroll_and_selection_across_refreshes(app, studio):
    from gmic_studio.core import filters
    fs = [filters.FilterDef(("Cat %02d" % c,), "Filter %02d-%02d" % (c, n), "blur") for c in range(12) for n in range(8)]
    ready(app, studio)
    studio.index = filters.FilterIndex(fs)
    d = _make_docker(app, studio, doc=False)
    d.resize(380, 520)
    d.show()
    app.processEvents()

    def group(label):
        for i in range(d.tree.topLevelItemCount()):
            if d.tree.topLevelItem(i).text(0) == label:
                return d.tree.topLevelItem(i)

    for label in ("Cat 02", "Cat 09"):
        group(label).setExpanded(True)
    app.processEvents()
    target = group("Cat 09").child(3)
    d.tree.setCurrentItem(target)                                  # a user click: selects and remembers its category
    d.tree.scrollToItem(target)
    app.processEvents()
    bar = d.tree.verticalScrollBar()
    scrolled = bar.value()
    assert scrolled > 0 and d.fdef.id == "Cat 09 > Filter 09-03"

    for n, refresh in enumerate((lambda: d._on_busy(False), d._refresh_lists, lambda: d._toggle_favorite())):
        studio.settings.set("favorites", [d.fdef.id] if n < 2 else [])
        refresh()
        for _ in range(3):
            app.processEvents()
        assert group("Cat 02").isExpanded() and group("Cat 09").isExpanded() and not group("Cat 05").isExpanded()
        assert abs(bar.value() - scrolled) <= 2, (bar.value(), scrolled)                 # the view did not jump to the top
        cur = d.tree.currentItem()
        assert d._key(cur) == "Cat 09 > Filter 09-03" and cur.parent().text(0) == "Cat 09"
        assert all(not d.tree.topLevelItem(i).text(0).endswith("Favorites") for i in range(d.tree.topLevelItemCount()))
    assert d.fdef.id == "Cat 09 > Filter 09-03"


def test_searching_and_clearing_restores_the_open_groups(app, studio):
    from gmic_studio.core import filters
    ready(app, studio)
    studio.index = filters.FilterIndex([filters.FilterDef(("Cat %d" % c,), "F%d%d" % (c, n), "blur") for c in range(4) for n in range(3)])
    d = _make_docker(app, studio, doc=False)
    d.show()
    d.tree.topLevelItem(d.tree.topLevelItemCount() - 1).setExpanded(True)
    last = d.tree.topLevelItem(d.tree.topLevelItemCount() - 1).text(0)
    d.search.setText("F1")
    assert d.tree.topLevelItemCount() >= 3 and d.tree.topLevelItem(0).childCount() == 0     # flat results
    d.search.setText("")
    opened = [d.tree.topLevelItem(i).text(0) for i in range(d.tree.topLevelItemCount()) if d.tree.topLevelItem(i).isExpanded()]
    assert opened == [last]


# -- recent filters: a button next to the search box -------------------------------------------
def test_tree_has_no_recent_group_and_the_recent_button_lists_them(app, studio):
    from gmic_studio.core import filters
    ready(app, studio)
    studio.index = filters.FilterIndex([filters.FilterDef(("Cat %d" % c,), "F%d%d" % (c, n), "blur") for c in range(3) for n in range(3)])
    studio.settings.set("recents", ["Cat 2 > F21", "Cat 0 > F00", "Gone > Missing"])
    d = _make_docker(app, studio, doc=False)
    assert [d.tree.topLevelItem(i).text(0) for i in range(d.tree.topLevelItemCount())] == ["Cat 0", "Cat 1", "Cat 2"]
    assert d.recent.isEnabled() and d.recent.labels() == ["F21  ·  Cat 2", "F00  ·  Cat 0"]     # newest first, unknown skipped
    assert [a.text() for a in d.recent.menu().actions() if a.text()] == d.recent.labels() + ["Clear recent filters"]
    studio.settings.set("recents", [])
    d._refresh_lists()
    assert not d.recent.isEnabled()


def test_choosing_a_recent_filter_opens_it_in_place_and_loads_it(app, studio):
    from gmic_studio.core import filters
    ready(app, studio)
    studio.index = filters.FilterIndex([filters.FilterDef(("Cat %d" % c,), "F%d%d" % (c, n), "blur", []) for c in range(3) for n in range(3)])
    studio.settings.set("recents", ["Cat 2 > F21"])
    d = _make_docker(app, studio, doc=False)
    d.tabs.setCurrentIndex(PRESETS)
    d.search.setText("zzz")                                            # something typed: choosing a recent clears it
    d.recent.menu().actions()[0].trigger()                             # what clicking the menu entry does
    assert d.search.text() == "" and d.tabs.currentIndex() == 0
    assert d.fdef.id == "Cat 2 > F21" and d.tree.currentItem().parent().text(0) == "Cat 2"
    assert d.tree.currentItem().parent().isExpanded()                  # opened so the filter is visible
    assert not d.recent.choose("not a known label") and d.fdef.id == "Cat 2 > F21"


def test_a_new_recent_appears_in_the_menu_after_apply(app, studio):
    d = _make_docker(app, studio)
    node = krita.Node("L", 6, 4)
    doc = use_doc(node)
    assert d.recent.labels() == [] and not d.recent.isEnabled()
    d._show_filter(studio.index.get("Basics > Sepia"), None)
    d._apply()
    wait(app, lambda: not studio.busy and doc.node is not node)
    app.processEvents()
    assert d.recent.labels() == ["Sepia  ·  Basics"] and d.recent.isEnabled()


def _key(widget, key):
    from gmic_studio.compat import QtCore, QtGui, enum
    ev = QtGui.QKeyEvent(enum(QtCore.QEvent, "Type", "KeyPress"), enum(QtCore.Qt, "Key", key), enum(QtCore.Qt, "KeyboardModifier", "NoModifier"))
    widget.keyPressEvent(ev)


def test_escape_clears_the_search_and_then_releases_focus(app, studio):
    d = _make_docker(app, studio, doc=False)
    d.show()
    d.activateWindow()
    d.search.setFocus()
    app.processEvents()
    d.search.setText("blur")
    _key(d.search, "Key_Escape")
    assert d.search.text() == ""                                        # first Esc: clear the search
    released = []
    d.search.clearFocus = lambda: released.append(True)                 # (focus cannot be asserted on an offscreen display)
    _key(d.search, "Key_Escape")
    assert released == [True]                                           # second Esc: give the focus back


def test_enter_in_the_search_box_moves_to_the_results(app, studio):
    d = _make_docker(app, studio, doc=False)
    d.show()
    focused = []
    d.tree.setFocus = lambda *a: focused.append("tree")
    d.plist.setFocus = lambda *a: focused.append("list")
    d.search.setText("blur")
    d.search.returnPressed.emit()
    assert focused == ["tree"] and d.tree.currentItem() is not None            # arrow keys now walk the matches
    d.tabs.setCurrentIndex(PRESETS)
    studio.save_preset("P", studio.index.get("Basics > Blur"), {})
    d.search.setText("")
    d.search.returnPressed.emit()
    assert focused == ["tree", "list"] and d.plist.currentRow() == 0


def test_clicking_empty_space_can_take_focus_away_from_the_search_box(app, studio):
    from gmic_studio.compat import QtCore, enum
    click = enum(QtCore.Qt, "FocusPolicy", "ClickFocus")
    d = _make_docker(app, studio, doc=False)
    assert d.widget().focusPolicy() == click and d.preview.focusPolicy() == click


# -- preview commands: the filter's own preview draws preview-only extras ---------------------------------------
def _spy_jobs(monkeypatch):
    jobs = []
    real = engine_mod.Runner

    class Spy(real):
        def __init__(self, exe, job, timeout=600):
            jobs.append(job)
            super().__init__(exe, job, timeout)

    monkeypatch.setattr(engine_mod, "Runner", Spy)
    return jobs


def _fdef(**kw):
    from gmic_studio.core import filters
    base = dict(path=("T",), name="Brush", command="blur", params=[])
    base.update(kw)
    return filters.FilterDef(**base)


def test_preview_uses_the_preview_command_and_apply_the_normal_one(app, studio, monkeypatch):
    ready(app, studio)
    node = krita.Node("L", 8, 8)
    doc = use_doc(node)
    jobs = _spy_jobs(monkeypatch)
    f = _fdef(preview_command="sharpen")
    studio.request_preview(f, {})
    wait(app, lambda: len(jobs) == 1)
    assert jobs[0].command == "sharpen"                              # preview: the filter's own preview command
    wait(app, lambda: not studio._threads)
    studio.apply(f, {})
    wait(app, lambda: not studio.busy and doc.node is not node)
    assert jobs[-1].command == "blur"                                # apply: never the preview command


def test_a_failing_preview_command_falls_back_to_the_normal_one_once(app, studio, monkeypatch):
    ready(app, studio)
    use_doc(krita.Node("L", 8, 8))
    jobs = _spy_jobs(monkeypatch)
    got = []
    studio.previewReady.connect(lambda b, a: got.append(1))
    f = _fdef(preview_command="fail")                                # the fake gmic fails on "fail[...]"
    studio.request_preview(f, {})
    wait(app, lambda: got)
    assert [j.command for j in jobs] == ["fail", "blur"] and f.id in studio._bad_previews
    wait(app, lambda: not studio._threads)
    studio.request_preview(f, {})
    wait(app, lambda: len(got) == 2)
    assert [j.command for j in jobs] == ["fail", "blur", "blur"]     # remembered: no second failed attempt


@pytest.mark.skipif(__import__("shutil").which("gmic") is None, reason="needs a real gmic")
def test_real_gmic_preview_command_draws_extras_that_apply_never_has(app, tmp_path):
    """Like Brushify's 'Preview Brush' inset: only the preview shows it."""
    from gmic_studio.compat import QtGui
    import gmic_studio.studio as st
    home = tmp_path / "rg_home"
    home.mkdir()
    defs = tmp_path / "inset.gmic"
    defs.write_text('#@gui Test > Inset : fx_t_inset, fx_t_inset_preview(0)\n'
                    '#@gui : Amount = int(10,0,100)\n'
                    'fx_t_inset : add $1\n'
                    'fx_t_inset_preview : fx_t_inset $* fill[0] "x<8&&y<8?255:i"\n')
    os.environ["GMIC_STUDIO_HOME"] = str(home)
    st._instance = None
    studio = st.get_studio()
    studio.settings.set("definitions_path", str(defs))
    studio.refresh()
    wait(app, lambda: studio.exe and studio.index.get("Test > Inset"))
    f = studio.index.get("Test > Inset")
    assert f.preview_command == "fx_t_inset_preview"
    node = krita.Node("L", 20, 20, fill=bytes([100, 100, 100, 255]) * 400)
    doc = use_doc(node)
    after = []
    studio.previewReady.connect(lambda b, a: after.append(a))
    studio.request_preview(f, f.defaults())
    wait(app, lambda: after)
    px = lambda im, x, y: (lambda c: (round(c.redF() * 255), round(c.greenF() * 255), round(c.blueF() * 255), round(c.alphaF() * 255)))(im.pixelColor(x, y))
    assert px(after[0], 2, 2) == (255, 255, 255, 255)                # the preview-only square
    assert px(after[0], 15, 15) == (110, 110, 110, 255)              # the filter itself
    wait(app, lambda: not studio._threads)
    studio.apply(f, f.defaults())
    wait(app, lambda: not studio.busy and doc.node is not node)
    px0 = doc.node.pixels[(2 * 20 + 2) * 4:(2 * 20 + 2) * 4 + 4]
    assert tuple(px0) == (110, 110, 110, 255)                        # the applied result has no inset
    studio.wait_for_threads()


# -- favorites have their own tab; batch remove and batch delete --------------------------------------------------
def _favorite(d, studio, *names):
    for n in names:
        d._show_filter(studio.index.get("Basics > " + n), None)
        d.fav.click()


def test_favorites_tab_lists_only_favorites_and_loads_them(app, studio):
    d = _make_docker(app, studio)
    assert d.tabs.tabText(FAVORITES) == "Favorites (0)"
    _favorite(d, studio, "Sepia", "Blur")
    assert d.tabs.tabText(FAVORITES) == "Favorites (2)"
    assert [d.flist.item(i).text() for i in range(d.flist.count())] == ["Blur  ·  Basics", "Sepia  ·  Basics"]
    assert all("Favorites" not in d.tree.topLevelItem(i).text(0) for i in range(d.tree.topLevelItemCount()))
    d.tabs.setCurrentIndex(FAVORITES)
    d._show_filter(studio.index.get("Basics > Sharpen"), None)
    d.flist.setCurrentRow(1)
    assert d.fdef.id == "Basics > Sepia" and d.preset_id is None
    d.search.setText("blu")                                          # the search box narrows the favorites too
    assert [d.flist.item(i).text() for i in range(d.flist.count())] == ["Blur  ·  Basics"]
    d.search.setText("")
    d.fav.click()                                                    # the star on a favorite removes it
    assert d.tabs.tabText(FAVORITES) == "Favorites (1)"


def test_batch_remove_favorites(app, studio):
    d = _make_docker(app, studio)
    _favorite(d, studio, "Blur", "Sepia", "Sharpen", "Negative")
    d.tabs.setCurrentIndex(FAVORITES)
    shown = d.fdef.id
    d.flist.clearSelection()                                         # (the current filter is highlighted by default)
    d.flist.item(0).setSelected(True)
    d.flist.item(2).setSelected(True)
    assert d.fav_remove_btn.isEnabled() and d.fav_remove_btn.text() == "Remove from favorites (2)"
    assert d.fdef.id == shown                                        # picking several does not load each one
    d.fav_remove_btn.click()
    assert d.tabs.tabText(FAVORITES) == "Favorites (2)" and len(studio.settings.get("favorites")) == 2
    d.flist.selectAll()
    assert d.fav_remove_btn.text() == "Remove from favorites (2)"
    d.flist.deleteRequested.emit()                                   # what the Delete key does
    assert studio.settings.get("favorites") == [] and d.tabs.tabText(FAVORITES) == "Favorites (0)"
    assert not d.fav_remove_btn.isEnabled() and d.fav_remove_btn.text() == "Remove from favorites"


def test_batch_delete_presets_asks_once_and_deletes_them_all(app, studio, monkeypatch):
    d = _make_docker(app, studio)
    blur = studio.index.get("Basics > Blur")
    for n in "ABCDE":
        studio.save_preset(n, blur, {"0": 1.0})
    d.tabs.setCurrentIndex(PRESETS)
    asked = []
    monkeypatch.setattr(d, "_confirm", lambda text: asked.append(text) or False)
    d.plist.selectAll()
    assert d.preset_delete_btn.text() == "Delete selected (5)"
    d.preset_delete_btn.click()
    assert asked == ["Delete these 5 presets?"] and len(studio.presets) == 5            # declined: nothing happens
    monkeypatch.setattr(d, "_confirm", lambda text: asked.append(text) or True)
    d.preset_delete_btn.click()
    assert len(studio.presets) == 0 and d.plist.count() == 0 and d.tabs.tabText(PRESETS) == "Presets (0)"
    assert not d.preset_delete_btn.isEnabled()


def test_batch_delete_a_subset_and_the_loaded_preset(app, studio, monkeypatch):
    d = _make_docker(app, studio)
    blur = studio.index.get("Basics > Blur")
    for n in "ABC":
        studio.save_preset(n, blur, {"0": float("ABC".index(n) + 1)})
    d.tabs.setCurrentIndex(PRESETS)
    d.plist.setCurrentRow(0)                                          # loads preset A
    assert d.preset_id is not None
    loaded = d.preset_id
    d.plist.clearSelection()
    d.plist.item(0).setSelected(True)
    d.plist.item(1).setSelected(True)
    texts = []
    monkeypatch.setattr(d, "_confirm", lambda text: texts.append(text) or True)
    d._delete_selected_presets()
    assert len(studio.presets) == 1 and studio.presets.all()[0]["name"] == "C" and d.preset_id is None
    d.plist.setCurrentRow(0)
    d.plist.item(0).setSelected(True)
    d._delete_selected_presets()
    assert texts[-1] == 'Delete the preset "C"?' and len(studio.presets) == 0           # one preset: it names it
    assert studio.presets.get(loaded) is None


# -- randomizer on the filter's options ------------------------------------------------------------------------
def _rand_filter():
    from gmic_studio.core import filters
    return filters.parse_definitions(
        "#@gui T > Rand : cmd\n#@gui : Size = float(5,0,10)\n#@gui : Count = int(3,1,9)\n#@gui : Mode = choice(0,\"a\",\"b\",\"c\")\n"
        "#@gui : Name = text(\"keep\")\n")[0]


def test_randomize_button_changes_unlocked_values_and_the_seed(app, studio):
    d = _make_docker(app, studio, doc=False)
    f = _rand_filter()
    d._show_filter(f, None)
    studio.seed = 5
    changed = set()
    for _ in range(25):
        d.rand_btn.click()
        v = d.form.values()
        changed |= {k for k in ("0", "1", "2") if v[k] != f.defaults()[k]}
        assert 0 <= v["0"] <= 10 and 1 <= v["1"] <= 9 and v["2"] in (0, 1, 2) and v["3"] == "keep"
    assert changed == {"0", "1", "2"} and studio.seed != 5
    assert d.status.toolTip().startswith("Randomized")


def test_locked_values_survive_randomize_and_nudge_keeps_the_seed(app, studio):
    d = _make_docker(app, studio, doc=False)
    f = _rand_filter()
    d._show_filter(f, {"0": 7.0, "1": 4, "2": 1, "3": "keep"})
    label = d.form._labels[0]
    label.mouseDoubleClickEvent(None)                                  # double-click the name: lock it
    assert d.form.locked == {"0"} and label.locked and label.text().startswith("\U0001F512")
    studio.seed = 99
    for _ in range(20):
        d._randomize(1.0)
        assert d.form.values()["0"] == 7.0
    assert "kept 1 locked" in d.status.toolTip()
    studio.seed = 99
    before = d.form.values()["1"]
    d._randomize(0.1)
    assert studio.seed == 99                                           # a nudge keeps random filters recognisable
    assert abs(d.form.values()["1"] - before) <= 1                     # ...and stays near where it was (10% of 1..9)
    d._build_rand_menu()
    unlock = [a for a in d.rand_menu.actions() if a.text().startswith("Unlock all")][0]
    assert unlock.isEnabled() and "1 locked" in unlock.text()
    unlock.trigger()
    assert d.form.locked == set() and not label.locked and not label.text().startswith("\U0001F512")
    d._build_rand_menu()
    assert not [a for a in d.rand_menu.actions() if a.text().startswith("Unlock all")][0].isEnabled()
    assert [a.text() for a in d.rand_menu.actions() if a.text() and not a.text().startswith("Unlock")] == [
        "Randomize everything (new seed)", "Nudge a little (about 15%)", "Nudge more (about 35%)", "New random seed only"]


def test_locks_are_remembered_per_filter(app, studio):
    d = _make_docker(app, studio, doc=False)
    f = _rand_filter()
    d._show_filter(f, None)
    d.form._labels[1].set_locked(True)
    d._show_filter(studio.index.get("Basics > Blur"), None)
    assert d.form.locked == set()
    d._show_filter(f, None)
    assert d.form.locked == {"1"} and d.form._labels[1].locked


# -- the layout never shifts --------------------------------------------------------------------------------------
def test_busy_indicator_and_long_messages_do_not_move_anything(app, studio):
    from gmic_studio.compat import QtCore
    d = _make_docker(app, studio, doc=False)
    d.resize(340, 900)
    d.show()
    for _ in range(5):
        app.processEvents()
    y = lambda w: w.mapTo(d.widget(), QtCore.QPoint(0, 0)).y()
    before = (y(d.preview), y(d.status), y(d.apply_btn), d.preview.height(), d.status.height(), d.widget().minimumSizeHint().height())
    studio.previewBusy.emit(True)
    studio.status.emit("An error with a really long explanation " * 20, True)
    for _ in range(3):
        app.processEvents()
    assert d.preview.busy
    assert (y(d.preview), y(d.status), y(d.apply_btn), d.preview.height(), d.status.height(), d.widget().minimumSizeHint().height()) == before
    assert d.status.text().endswith(("…", "...")) and len(d.status.text()) < 100                      # shortened to the one line it has
    assert d.status.toolTip().startswith("⚠ An error") and len(d.status.toolTip()) > 400    # the whole text on hover
    studio.previewBusy.emit(False)
    studio.status.emit("Short.", False)
    app.processEvents()
    assert (y(d.preview), y(d.status), y(d.apply_btn), d.preview.height(), d.status.height()) == before[:5]
    assert d.status.height() <= d.status.fontMetrics().lineSpacing() + 6                     # exactly one line: no reserved empty space


def test_preview_busy_bar_is_drawn_on_the_bottom_edge(app):
    from gmic_studio.compat import QtGui
    from gmic_studio.preview_view import PreviewView
    view = PreviewView()
    view.resize(200, 120)
    view.show()
    quiet = view.grab().toImage()
    view.set_busy(True)
    view._phase = 0.5
    busy = view.grab().toImage()
    view.set_busy(False)
    strip = lambda im: [im.pixelColor(x, im.height() - 2).name() for x in range(0, im.width(), 10)]
    assert strip(busy) != strip(quiet)                                                       # something is drawn there
    assert [busy.pixelColor(x, 20).name() for x in range(0, 200, 20)] == [quiet.pixelColor(x, 20).name() for x in range(0, 200, 20)]


# -- slow previews --------------------------------------------------------------------------------------------------
def test_slow_filters_get_smaller_previews_and_refresh_renders_full_size(app, studio):
    ready(app, studio)
    node = krita.Node("L", 700, 400)
    use_doc(node)
    f = studio.index.get("Basics > Blur")
    studio.settings.note_preview_time(f.id, 99.0)                      # it was slow before
    msgs, shown = [], []
    studio.status.connect(lambda m, e: msgs.append(m))
    studio.previewReady.connect(lambda b, a: shown.append((b.width(), b.height())))
    studio.request_preview(f, f.defaults())
    wait(app, lambda: shown)
    assert shown[-1] == (512, 293) and any("fast, smaller image" in m for m in msgs)
    wait(app, lambda: not studio._threads)
    studio.request_preview(f, f.defaults(), force_full=True)           # what the Refresh button does
    wait(app, lambda: len(shown) == 2)
    assert shown[-1] == (700, 400) and "whole image" in msgs[-1]
    assert not studio.settings.is_slow(f.id)                           # it turned out fast: no longer treated as slow


def test_a_slow_whole_image_preview_is_remembered_and_explained(app, studio):
    ready(app, studio)
    use_doc(krita.Node("L", 40, 40))
    studio.settings.set("slow_preview_s", 0.0)                          # every preview now counts as slow
    f = studio.index.get("Basics > Sepia")
    msgs = []
    studio.status.connect(lambda m, e: msgs.append(m))
    studio.request_preview(f, f.defaults())
    wait(app, lambda: any(m.startswith("Preview ready") for m in msgs))
    assert studio.settings.is_slow(f.id) and "smaller image" in msgs[-1] and "Refresh renders full size" in msgs[-1]


def test_elapsed_time_is_shown_while_a_preview_renders(app, studio):
    import time as _t
    d = _make_docker(app, studio, doc=False)
    d._on_status("Previewing (whole image)...", False)
    d._on_preview_busy(True)
    d._pv_started = _t.time() - 7.4
    d._pv_tick()
    assert d.status.full_text() == "Previewing (whole image)  7 s"
    d._on_preview_busy(False)
    assert not d._pv_timer.isActive()


def test_speed_settings_are_saved_and_slow_filters_can_be_forgotten(app, studio):
    from gmic_studio.settings_dialog import SettingsDialog
    studio.settings.set("slow_filters", ["A", "B"])
    dlg = SettingsDialog(studio)
    assert dlg.full_mp.value() == 3.0 and dlg.fast.isChecked() and dlg.forget.text() == "Forget slow filters (2)"
    dlg.full_mp.setValue(1.5)
    dlg.slow_s.setValue(12)
    dlg.fast.setChecked(False)
    dlg.forget.click()
    dlg._accept(close=False)
    s = studio.settings
    assert (s.get("preview_full_mp"), s.get("slow_preview_s"), s.get("fast_slow_previews"), s.get("slow_filters")) == (1.5, 12.0, False, [])


# -- presets stored inside the Krita document -----------------------------------------------------------------------
def _embedded(*items):
    import json
    presets = [{"id": "doc-%s" % n, "name": n, "filter": "Basics > Blur", "values": {"0": float(i + 1)}, "seed": None,
                "created": "2026-01-01T00:00", "used": ""} for i, n in enumerate(items)]
    return json.dumps({"version": 2, "presets": presets}).encode()


def _open_doc(*embedded_names, size=(12, 12)):
    node = krita.Node("L", *size)
    doc = krita.Document(node)
    if embedded_names:
        doc._annotations[bridge.PRESET_ANNOTATION] = _embedded(*embedded_names)
    krita.Krita.instance().doc = doc
    return doc


def test_presets_embedded_in_an_opened_document_appear_beside_the_library(app, studio):
    ready(app, studio)
    studio.save_preset("Mine", studio.index.get("Basics > Sepia"), {})
    _open_doc("From the file", "Another")
    changes = []
    studio.presetsChanged.connect(lambda: changes.append(1))
    assert studio.sync_document() is True and changes
    assert studio.presets.has_document
    got = {(p["name"], p["scope"]) for p in studio.presets.all()}
    assert got == {("Mine", "library"), ("From the file", "document"), ("Another", "document")}
    assert studio.sync_document() is False                                      # nothing changed: no churn


def test_saving_into_the_document_writes_the_annotation_and_marks_it_modified(app, studio, tmp_path):
    ready(app, studio)
    doc = _open_doc()
    studio.sync_document()
    blur = studio.index.get("Basics > Blur")
    assert not doc.modified()
    studio.save_preset("Only here", blur, {"0": 3.0}, lock_seed=False, scope="document")
    assert doc.modified() and bridge.PRESET_ANNOTATION in doc.annotationTypes()      # Krita will offer to save the file
    assert b"Only here" in doc.annotation(bridge.PRESET_ANNOTATION)
    assert not os.path.exists(studio.presets.path)                                   # the library file was not touched
    assert studio.settings.get("preset_scope") == "document"
    studio.save_preset("In library", blur, {}, scope="library")
    assert os.path.exists(studio.presets.path) and b"In library" not in doc.annotation(bridge.PRESET_ANNOTATION)
    assert studio.settings.get("preset_scope") == "library"


def test_reopening_the_file_brings_its_presets_back(app, studio):
    ready(app, studio)
    doc = _open_doc()
    studio.sync_document()
    studio.save_preset("Round trip", studio.index.get("Basics > Blur"), {"0": 5.5}, lock_seed=True, scope="document")
    saved = doc.annotation(bridge.PRESET_ANNOTATION)
    krita.Krita.instance().doc = None
    studio.sync_document()
    assert not studio.presets.has_document and len(studio.presets) == 0               # closed: nothing left behind
    reopened = _open_doc()
    reopened._annotations[bridge.PRESET_ANNOTATION] = saved                           # what Krita restores from the .kra
    studio.sync_document()
    p = studio.presets.find("Basics > Blur", "Round trip", "document")
    assert p and p["values"] == {"0": 5.5} and p["seed"] == studio.seed


def test_switching_documents_swaps_the_embedded_presets(app, studio):
    ready(app, studio)
    first = _open_doc("One")
    studio.sync_document()
    assert [p["name"] for p in studio.presets.all()] == ["One"]
    second = _open_doc("Two", "Three")
    assert studio.sync_document() is True
    assert sorted(p["name"] for p in studio.presets.all()) == ["Three", "Two"]
    studio.save_preset("Added to second", studio.index.get("Basics > Blur"), {}, scope="document")
    assert b"Added to second" in second.annotation(bridge.PRESET_ANNOTATION)
    assert b"Added to second" not in first.annotation(bridge.PRESET_ANNOTATION)        # never leaks into the other file
    krita.Krita.instance().doc = first
    studio.sync_document()
    assert [p["name"] for p in studio.presets.all()] == ["One"]


def test_removing_the_last_embedded_preset_removes_the_annotation(app, studio):
    ready(app, studio)
    doc = _open_doc("Only")
    studio.sync_document()
    assert studio.delete_presets(["doc-Only"]) == 1
    assert bridge.PRESET_ANNOTATION not in doc.annotationTypes() and doc.modified()


def test_docker_saves_lists_filters_and_copies_between_sources(app, studio, monkeypatch):
    d = _make_docker(app, studio)
    _open_doc()
    studio.sync_document()
    blur = studio.index.get("Basics > Blur")
    d._show_filter(blur, None)
    d.form.set_values({"0": 6.0})
    _stub_save_dialog(monkeypatch, "Doc look", False, "document")
    d._save_preset()
    _stub_save_dialog(monkeypatch, "Lib look", False, "library")
    d.preset_id = None
    d._save_preset()
    names = lambda: sorted(d.plist.item(i).text() for i in range(d.plist.count()))
    assert names() == ["Doc look  ·  Blur   [document]", "Lib look  ·  Blur"]
    d.scope_box.setCurrentIndex(d.scope_box.findData("document"))
    assert names() == ["Doc look  ·  Blur   [document]"]
    d.scope_box.setCurrentIndex(d.scope_box.findData("library"))
    assert names() == ["Lib look  ·  Blur"]
    d.scope_box.setCurrentIndex(0)
    lib_id = studio.presets.find(blur.id, "Lib look")["id"]
    doc_id = studio.presets.find(blur.id, "Doc look", "document")["id"]
    menu = W_menu()
    d._preset_menu(menu, lib_id)
    assert "Copy to this document" in [a.text() for a in menu.actions()]
    menu = W_menu()
    d._preset_menu(menu, doc_id)
    assert "Copy to my library" in [a.text() for a in menu.actions()]
    d._copy_presets([doc_id], "library")
    assert studio.presets.find(blur.id, "Doc look")["scope"] == "library" and len(studio.presets) == 3
    d._copy_presets([lib_id], "document")
    assert len(studio.presets) == 4 and studio.presets.find(blur.id, "Lib look", "document")
    assert b"Lib look" in krita.Krita.instance().doc.annotation(bridge.PRESET_ANNOTATION)     # really inside the document


def W_menu():
    from gmic_studio.compat import QtWidgets
    return QtWidgets.QMenu()


def test_no_document_means_no_document_choices(app, studio):
    from gmic_studio.preset_ui import PresetSaveDialog
    d = _make_docker(app, studio, doc=False)
    krita.Krita.instance().doc = None
    studio.sync_document()
    dlg = PresetSaveDialog(None, "Blur", "", "/x/lib.json", can_document=False, scope="document")
    assert not dlg.doc_check.isEnabled() and dlg.scopes() == ["library"] and "/x/lib.json" in dlg.where.text()
    menu = W_menu()
    studio.save_preset("Lib only", studio.index.get("Basics > Blur"), {})
    d._preset_menu(menu, studio.presets.all()[0]["id"])
    assert not [a for a in menu.actions() if a.text().startswith("Copy")]             # nowhere to copy to


def test_the_save_dialog_lets_you_pick_either_place_or_both(app, studio):
    from gmic_studio.preset_ui import PresetSaveDialog
    save = lambda dlg: dlg.buttons.button(dlg.buttons.StandardButton.Save if hasattr(dlg.buttons, "StandardButton") else dlg.buttons.Save)
    dlg = PresetSaveDialog(None, "Blur", "", "/x/lib.json", can_document=True, scope="library")
    assert dlg.scopes() == ["library"] and dlg.lib_check.isEnabled() and dlg.doc_check.isEnabled()
    dlg.doc_check.setChecked(True)
    assert dlg.scopes() == ["library", "document"]                                    # both at once
    assert "/x/lib.json" in dlg.where.text() and ".kra" in dlg.where.text()
    dlg.lib_check.setChecked(False)
    assert dlg.scopes() == ["document"] and "/x/lib.json" not in dlg.where.text()
    dlg.doc_check.setChecked(False)
    assert dlg.scopes() == [] and not save(dlg).isEnabled() and "Tick at least one" in dlg.where.text()
    dlg.edit.setText("n")
    dlg._ok()
    assert dlg.result() == 0                                                          # nothing ticked: it will not close
    dlg.lib_check.setChecked(True)
    assert save(dlg).isEnabled()
    assert PresetSaveDialog(None, "Blur", "", "/x", can_document=True, scope="both").scopes() == ["library", "document"]
    assert PresetSaveDialog(None, "Blur", "", "/x", can_document=True, scope="document").scopes() == ["document"]


def test_saving_to_both_places_writes_both_and_remembers_the_choice(app, studio, monkeypatch):
    d = _make_docker(app, studio)
    doc = _open_doc()
    studio.sync_document()
    blur = studio.index.get("Basics > Blur")
    d._show_filter(blur, None)
    d.form.set_values({"0": 4.0})
    studio.seed = 1234
    _stub_save_dialog(monkeypatch, "Both", True, "both")
    d._save_preset()
    lib, indoc = studio.presets.find(blur.id, "Both"), studio.presets.find(blur.id, "Both", "document")
    assert lib["values"] == indoc["values"] == {"0": 4.0} and lib["seed"] == indoc["seed"] == 1234 and lib["id"] != indoc["id"]
    assert b"Both" in doc.annotation(bridge.PRESET_ANNOTATION) and os.path.exists(studio.presets.path)
    assert d.preset_id == lib["id"] and studio.settings.get("preset_scope") == "both"
    assert d.status.toolTip() == 'Saved preset "Both" in your library and this document.'
    assert len(studio.presets) == 2
    asked = []
    monkeypatch.setattr(d, "_confirm", lambda text: asked.append(text) or False)
    d._save_preset()                                                                  # same name again: asks, naming both places
    assert asked == ['Replace the preset "Both" in your library and this document?'] and len(studio.presets) == 2


def test_a_failing_document_write_is_reported_not_swallowed(app, studio, monkeypatch):
    d = _make_docker(app, studio)
    doc = _open_doc()
    studio.sync_document()
    d._show_filter(studio.index.get("Basics > Blur"), None)
    _stub_save_dialog(monkeypatch, "Doomed", False, "document")
    monkeypatch.setattr(doc, "setAnnotation", lambda *a: (_ for _ in ()).throw(RuntimeError("document is closed")))
    d._save_preset()
    assert "Could not save the preset" in d.status.toolTip() and "document is closed" in d.status.toolTip()


def test_a_preset_from_a_closed_document_is_forgotten_by_the_docker(app, studio):
    d = _make_docker(app, studio, doc=False)
    _open_doc("Temp")
    studio.sync_document()
    d.tabs.setCurrentIndex(PRESETS)
    d.plist.setCurrentRow(0)
    assert d.preset_id == "doc-Temp"
    krita.Krita.instance().doc = None
    studio.sync_document()
    assert d.preset_id is None and d.plist.count() == 0 and d.tabs.tabText(PRESETS) == "Presets (0)"


def test_preview_draws_its_own_zoom_badge(app):
    from gmic_studio.compat import QtGui
    from gmic_studio.preview_view import PreviewView
    view = PreviewView()
    view.resize(200, 200)
    img = QtGui.QImage(100, 100, QtGui.QImage.Format.Format_RGBA8888 if hasattr(QtGui.QImage, "Format") else QtGui.QImage.Format_RGBA8888)
    img.fill(QtGui.QColor(255, 255, 255))
    view.show_image(img)
    shot = view.grab().toImage()
    white = shot.pixelColor(10, 10)
    badge = shot.pixelColor(shot.width() - 12, shot.height() - 12)
    assert white.red() >= 250 and badge.red() < 200                      # the dark badge sits over the bottom-right corner


def test_the_docker_has_no_separate_zoom_or_seed_or_background_rows(app, studio):
    d = _make_docker(app, studio, doc=False)
    assert not hasattr(d, "zoom_label") and d.dice.text() != ''
    texts = [a.text() for a in d.opts_menu.actions() if a.text()]
    assert texts == ["Force transparent background", "Refresh when the layer changes"]
    d.watch_act.setChecked(False)
    assert studio.settings.get("watch_layer") is False
    d.watch_act.setChecked(True)
    assert studio.settings.get("watch_layer") is True
    assert not hasattr(d, "prev_btn") and not hasattr(d, "next_btn") and d.tabs.cornerWidget() is None   # no arrow buttons


# -- point parameters: slider rows and draggable handles on the preview -------------------------------------------
def _point_filter():
    from gmic_studio.core import filters
    return filters.parse_definitions(
        "#@gui T > Pts : cmd\n#@gui : Size = float(5,0,10)\n#@gui : Axis = point(80,50,0,1,255,0,0,255)\n"
        "#@gui : Other = point(20,30,0,1,0,255,0,255)\n")[0]


def test_point_parameters_are_sliders_not_bare_number_boxes(app):
    from gmic_studio.param_form import ParamForm, PointEditor
    form = ParamForm()
    form.set_filter(_point_filter())
    from gmic_studio.compat import QtWidgets, enum
    editor = form._form.itemAt(1, enum(QtWidgets.QFormLayout, "ItemRole", "FieldRole")).widget()
    assert isinstance(editor, PointEditor)
    assert editor.x.slider.isEnabled() and editor.value() == [80.0, 50.0]
    editor.x.slider.setValue(editor.x.slider.maximum() // 4)                    # a quarter along
    assert abs(editor.value()[0] - 25.0) < 0.2 and abs(form.values()["1"][0] - 25.0) < 0.2
    editor.set_value([150.0, -20.0])                                            # beyond the slider, inside the number box
    assert editor.value() == [150.0, -20.0]
    changes = []
    form.changed.connect(lambda: changes.append(1))
    form.set_point(2, 10.0, 90.0)
    assert form.values()["2"] == [10.0, 90.0] and changes
    pts = form.points()
    assert [(p["index"], p["label"], p["color"]) for p in pts] == [(1, "Axis", [255, 0, 0]), (2, "Other", [0, 255, 0])]
    assert form.points()[0]["color"] == [255, 0, 0] and ParamForm().points() == []


def _preview_with_handles(app):
    from gmic_studio.compat import QtGui
    from gmic_studio.preview_view import PreviewView
    view = PreviewView()
    view.resize(400, 300)
    view.show()
    img = QtGui.QImage(200, 100, QtGui.QImage.Format.Format_RGBA8888 if hasattr(QtGui.QImage, "Format") else QtGui.QImage.Format_RGBA8888)
    img.fill(QtGui.QColor(90, 90, 90))
    view.show_image(img)                                                    # fits: zoom 2, 400x200, offset (0, 50)
    view.set_handles([{"index": 1, "x": 50.0, "y": 50.0, "color": [255, 0, 0]},
                      {"index": 2, "x": 100.0, "y": 0.0, "color": [0, 255, 0]}])
    return view


def _qtest():
    try:
        from PyQt6 import QtTest
    except ImportError:
        from PyQt5 import QtTest
    return QtTest.QTest


def none_button():
    from gmic_studio.compat import QtCore, enum
    return enum(QtCore.Qt, "MouseButton", "NoButton")


def _click(widget, kind, x, y):
    from gmic_studio.compat import QtCore, enum
    left, none = enum(QtCore.Qt, "MouseButton", "LeftButton"), enum(QtCore.Qt, "KeyboardModifier", "NoModifier")
    t, pos = _qtest(), QtCore.QPoint(int(x), int(y))
    if kind == "press":
        t.mousePress(widget, left, none, pos)
    elif kind == "release":
        t.mouseRelease(widget, left, none, pos)
    elif kind == "double":
        t.mouseDClick(widget, left, none, pos)
    elif kind == "move":                       # QTest.mouseMove sends nothing under Qt 5's offscreen platform: send the event
        from gmic_studio.compat import QtGui, QtWidgets
        ev = QtGui.QMouseEvent(enum(QtCore.QEvent, "Type", "MouseMove"), QtCore.QPointF(x, y), none_button(), left, none)
        QtWidgets.QApplication.sendEvent(widget, ev)


def test_handles_are_where_the_percentages_say_and_can_be_grabbed(app):
    view = _preview_with_handles(app)
    h1, h2 = view._handles
    assert (view._handle_pos(h1).x(), view._handle_pos(h1).y()) == (200.0, 150.0)          # middle of the picture
    assert (view._handle_pos(h2).x(), view._handle_pos(h2).y()) == (400.0, 50.0)           # its top-right corner
    from gmic_studio.compat import QtCore
    assert view._handle_at(QtCore.QPointF(204, 146)) is h1 and view._handle_at(QtCore.QPointF(250, 150)) is None
    shot = view.grab().toImage()
    centre = shot.pixelColor(200, 150)
    assert centre.red() > 200 and centre.green() < 60 and centre.blue() < 60                       # drawn in its own color


def test_dragging_a_handle_reports_percentages_and_does_not_pan(app):
    view = _preview_with_handles(app)
    moves, zooms = [], []
    view.handleMoved.connect(lambda i, x, y: moves.append((i, round(x, 1), round(y, 1))))
    view.zoomChanged.connect(zooms.append)
    off = (view._off.x(), view._off.y())
    _click(view, "press", 200, 150)
    _click(view, "move", 100, 100)                                          # (100-0)/2/200 = 25 %, (100-50)/2/100 = 25 %
    _click(view, "move", 350, 125)                                          # 87.5 %, 37.5 %
    _click(view, "move", 999, -999)                                         # far outside: clamped to the picture
    _click(view, "release", 999, -999)
    assert moves[0] == (1, 25.0, 25.0) and moves[1] == (1, 87.5, 37.5) and moves[-1] == (1, 100.0, 0.0)
    assert (view._off.x(), view._off.y()) == off and not zooms               # the picture did not move or zoom
    view.set_handles([{"index": 1, "x": 10.0, "y": 10.0, "color": [1, 2, 3]}])           # after release it may be replaced
    assert view._handles[0]["x"] == 10.0
    _click(view, "press", 100, 100)                                          # empty space: that still pans
    _click(view, "move", 120, 130)
    _click(view, "release", 120, 130)
    assert (view._off.x(), view._off.y()) != off


def test_handles_are_not_replaced_while_one_is_being_dragged_and_double_click_does_not_zoom(app):
    view = _preview_with_handles(app)
    _click(view, "press", 200, 150)
    view.set_handles([])                                                     # a refresh arriving mid-drag is ignored
    assert len(view._handles) == 2
    _click(view, "release", 200, 150)
    z = view.zoom
    _click(view, "double", 200, 150)
    assert view.zoom == z                                                    # on a handle: the zoom toggle is skipped
    _click(view, "double", 20, 80)
    assert view.zoom != z


def test_docker_shows_handles_only_when_they_line_up_and_dragging_edits_the_parameters(app, studio):
    from gmic_studio.compat import QtGui
    d = _make_docker(app, studio, doc=False)
    f = _point_filter()
    d._show_filter(f, None)
    img = QtGui.QImage(50, 40, QtGui.QImage.Format.Format_RGBA8888 if hasattr(QtGui.QImage, "Format") else QtGui.QImage.Format_RGBA8888)
    img.fill(0)
    d.preview.show_image(img)
    studio.preview_whole = True
    d._on_preview(img, img)
    assert [h["index"] for h in d.preview._handles] == [1, 2]
    d.preview.handleMoved.emit(2, 25.0, 75.0)                               # what dragging the second handle does
    assert d.form.values()["2"] == [25.0, 75.0] and d.timer.isActive()      # parameters follow, and the preview will refresh
    assert next(h for h in d.preview._handles if h["index"] == 2)["x"] == 25.0
    studio.preview_whole = False                                            # a 1:1 crop: percentages mean something else
    d._on_preview(img, img)
    assert d.preview._handles == []
    studio.preview_whole = True
    d._on_preview(img, img.scaled(80, 40))                                  # the filter resized the picture: no match
    assert d.preview._handles == []
    d._show_filter(studio.index.get("Basics > Blur"), None)                 # a filter without points
    d._on_preview(img, img)
    assert d.preview._handles == []


def test_settings_dialog_uses_the_new_name_and_the_default(app, studio):
    from gmic_studio.settings_dialog import SettingsDialog
    dlg = SettingsDialog(studio)
    assert dlg.tbg.text().startswith("Force transparent background") and not dlg.tbg.isChecked()
    dlg.tbg.setChecked(True)
    dlg._accept(close=False)
    assert studio.settings.get("transparent_bg") is True


def test_recent_filters_can_be_cleared_from_the_menu(app, studio):
    ready(app, studio)
    studio.settings.set("recents", ["Basics > Blur", "Basics > Sepia"])
    d = _make_docker(app, studio, doc=False)
    assert d.recent.isEnabled() and len(d.recent.labels()) == 2
    clear = [a for a in d.recent.menu().actions() if a.text() == "Clear recent filters"][0]
    clear.trigger()
    assert studio.settings.get("recents") == [] and d.recent.labels() == [] and not d.recent.isEnabled()
    assert d.status.toolTip() == "Cleared the recent filters."
    assert Settings_reload_recents() == []


def Settings_reload_recents():
    from gmic_studio.core.settings import Settings
    return Settings().get("recents")                                       # it was written to disk, not just hidden


def test_the_gear_is_a_real_icon_of_a_usable_size(app, studio):
    d = _make_docker(app, studio, doc=False)
    assert d.gear.text() == "" and not d.gear.icon().isNull()
    assert d.gear.width() == d.gear.height() == d.recent.sizeHint().height()           # a square button, as tall as its neighbours
    img = d.gear.icon().pixmap(16, 16).toImage()
    opaque = [(x, y) for x in range(img.width()) for y in range(img.height()) if img.pixelColor(x, y).alpha() > 128]
    assert len(opaque) > 60                                                             # it fills the icon area, not a speck
    xs, ys = [p[0] for p in opaque], [p[1] for p in opaque]
    assert min(xs) <= 2 and max(xs) >= img.width() - 3 and min(ys) <= 2 and max(ys) >= img.height() - 3
    c = img.width() // 2
    assert img.pixelColor(c, c).alpha() < 60                                           # the hole in the middle


def test_icon_takes_the_requested_color(app):
    from gmic_studio.compat import QtGui
    from gmic_studio.icons import gear_icon
    img = gear_icon(QtGui.QColor(200, 30, 30)).pixmap(16, 16).toImage()
    solid = [img.pixelColor(x, y) for x in range(16) for y in range(16) if img.pixelColor(x, y).alpha() == 255]
    assert solid and all(c.red() == 200 and c.green() == 30 for c in solid)


# -- button parameters, visibility markers, realtime dragging, preview modes ---------------------------
_BUTTON_DEFS = '''#@gui T > Btn : cmd
#@gui : Amount = int(3,0,10)
#@gui : Reset Pose = button(0)
#@gui : Hidden Mode = choice(1,"a","b")_0
#@gui : note_hidden = note("explains the hidden one")_0
#@gui : Gray = int(2,0,5)_1
#@gui : sep = separator()
'''


def test_button_is_one_shot_hidden_rows_are_hidden_and_grayed_rows_disabled(app):
    from gmic_studio.param_form import ParamForm
    fdef = filters.parse_definitions(_BUTTON_DEFS)[0]
    assert [p.visibility for p in fdef.params] == [2, 2, 0, 0, 1, 2]
    form = ParamForm()
    form.show()
    pressed = []
    form.buttonPressed.connect(pressed.append)
    form.set_filter(fdef)
    btn = [b for b in form.findChildren(QtWidgets.QPushButton) if b.text() == "Reset Pose"][0]
    btn.click()
    assert pressed == [1]
    assert form.values()["1"] == 0 and form.run_values()["1"] == 1            # only the run started by the press sees it
    assert fdef.command_line(form.run_values())[1][1] == "1" and fdef.command_line(form.values())[1][1] == "0"
    form.release_buttons()
    assert form.run_values()["1"] == 0
    assert form.values()["2"] == 1                                            # hidden values are still sent
    hidden = [w for w in form.findChildren(QtWidgets.QComboBox)]
    assert hidden and not hidden[0].isVisibleTo(form)
    assert not [l for l in form.findChildren(QtWidgets.QLabel) if "explains" in l.text()][0].isVisibleTo(form)
    gray = [w for w in form.findChildren(QtWidgets.QSpinBox)]
    assert gray[0].isEnabled() and not gray[1].isEnabled()


def test_a_pressed_button_stays_part_of_apply_until_a_value_changes(app):
    from gmic_studio.param_form import ParamForm
    fdef = filters.parse_definitions(_BUTTON_DEFS)[0]
    form = ParamForm()
    form.set_filter(fdef)
    btn = [b for b in form.findChildren(QtWidgets.QPushButton) if b.text() == "Reset Pose"][0]
    assert form.apply_values()["1"] == 0
    btn.click()
    assert form.apply_values()["1"] == 1                                       # pressed, preview not run yet
    form.release_buttons()                                                     # the preview ran with it
    assert form.run_values()["1"] == 0 and form.apply_values()["1"] == 1       # Apply still repeats what the preview shows
    form.set_values({"0": 7})
    assert form.apply_values()["1"] == 0                                       # a changed value makes the button history stale


_POINT_DEFS = '''#@gui T > Pt : cmd
#@gui : A = int(3,0,10)
#@gui : P = point(50,50)
#@gui : N = note("x")
#@gui : Q = point(10,10)
#@gui : H = value(-1)
#@gui : Go = button(0)
'''


def test_values_a_filter_reports_back_move_only_what_it_changed_and_never_run_again(app):
    from gmic_studio.param_form import ParamForm
    fdef = filters.parse_definitions(_POINT_DEFS)[0]
    form = ParamForm()
    form.set_filter(fdef)
    changes = []
    form.changed.connect(lambda: changes.append(1))
    sent = form.values()
    form.set_values({"1": [20.0, 30.0]})                                       # the user dragged P after the preview started
    del changes[:]
    moved = form.apply_status("{3}{70,80}{15,15}{5}{1}", sent)
    assert moved and not changes                                               # nothing triggers another preview
    assert form.values()["1"] == [20.0, 30.0]                                  # the newer edit wins
    assert form.values()["3"] == [15.0, 15.0] and form.values()["4"] == "5" and form.values()["0"] == 3
    assert form.apply_status("{}{}{}{}{}", form.values()) is False             # empty groups: unchanged
    assert form.apply_status("{9}", sent) is True and form.values()["0"] == 9  # fewer groups than parameters is fine
    assert fdef.command_line(form.values())[1][3] == "5"                       # a reported hidden value is what gets sent


def test_visibility_marker_spreads_to_neighbouring_notes():
    fdef = filters.parse_definitions('''#@gui T > P : cmd
#@gui : n1 = note("before")
#@gui : A = int(1,0,5)_0*
#@gui : n2 = note("after")
#@gui : B = int(1,0,5)
#@gui : n3 = note("kept")
''')[0]
    assert [p.visibility for p in fdef.params] == [0, 0, 0, 2, 2]


def test_pressing_a_button_previews_even_when_live_is_off(app, studio):
    d = _make_docker(app, studio, doc=False)
    fdef = filters.parse_definitions(_BUTTON_DEFS)[0]
    d._show_filter(fdef, None)
    d.live.setChecked(False)
    seen = []
    d.studio.request_preview = lambda f, values, force_full=False, interactive=False: seen.append(dict(values))
    [b for b in d.form.findChildren(QtWidgets.QPushButton) if b.text() == "Reset Pose"][0].click()
    assert seen and seen[0]["1"] == 1
    d._run_preview()
    assert seen[1]["1"] == 0                                                  # released after the one run


def test_dragging_a_handle_renders_without_waiting_and_ends_with_a_full_render(app, studio):
    from gmic_studio.compat import QtGui
    d = _make_docker(app, studio, doc=False)
    d._show_filter(_point_filter(), None)
    calls = []
    d.studio.request_preview = lambda f, values, force_full=False, interactive=False: calls.append(interactive)
    d.preview._grab = {"index": 2}                                           # a handle is held
    d.form.set_point(2, 30.0, 40.0)
    assert calls == [True] and not d.timer.isActive()                         # immediate, small and fast
    d.preview._grab = None
    d.preview.dragFinished.emit()
    assert calls == [True, False]                                             # let go: one full-quality render


def test_interactive_previews_keep_only_the_newest_request_while_one_renders(app, studio):
    ready(app, studio)
    class Busy:
        def cancel(self):
            pass
    studio._preview_runner = Busy()                                           # a render is in progress
    f = studio.index.get("Basics > Blur")
    studio.request_preview(f, {"0": 1}, interactive=True)
    studio.request_preview(f, {"0": 2}, interactive=True)
    assert studio._pending_preview[1] == {"0": 2} and studio._preview_runner is not None
    studio.cancel_preview()
    assert studio._pending_preview is None


def test_preview_view_keeps_the_view_when_the_same_picture_changes_resolution(app):
    from gmic_studio.compat import QtGui
    from gmic_studio.preview_view import PreviewView
    fmt = QtGui.QImage.Format.Format_RGBA8888 if hasattr(QtGui.QImage, "Format") else QtGui.QImage.Format_RGBA8888
    v = PreviewView()
    v.resize(400, 300)
    big = QtGui.QImage(800, 400, fmt)
    big.fill(0)
    v.show_image(big)
    v.zoom_to(1.0)
    v.show_image(big.scaled(400, 200))                                        # fast preview: half the pixels
    assert abs(v.zoom - 2.0) < 1e-6                                           # still the same part of the picture
    v.show_image(big.scaled(300, 300))                                        # a different picture shape: refit
    assert abs(v.zoom - min(400 / 300.0, 300 / 300.0)) < 0.05


def test_duplicate_modes_have_a_draggable_divider_and_accept_a_result_of_another_size(app, studio):
    from gmic_studio.compat import QtCore, QtGui
    from gmic_studio.preview_view import PreviewView
    fmt = QtGui.QImage.Format.Format_RGBA8888 if hasattr(QtGui.QImage, "Format") else QtGui.QImage.Format_RGBA8888
    img = QtGui.QImage(100, 100, fmt)
    img.fill(QtGui.QColor(0, 0, 255))
    v = PreviewView()
    v.resize(100, 100)
    v.show_image(img)
    v.fit()
    v.set_compare(None, 5, (50.0, 50.0))                                      # Duplicate Top: only the divider is drawn here
    assert v._before is None and v._mode == 5
    moved, released = [], []
    v.splitMoved.connect(lambda x, y: moved.append((round(x), round(y))))
    v.splitReleased.connect(lambda: released.append(1))
    c = v._split_pos()
    _click(v, "press", c.x(), c.y())
    _click(v, "move", c.x(), v._off.y() + v._zoom * 20)
    _click(v, "release", c.x(), v._off.y() + v._zoom * 20)
    assert moved and moved[-1][1] == 20 and released                          # dragged to 20 %, then a new render is asked for
    v.set_compare(None, 0)
    assert v._mode == 0
    d = _make_docker(app, studio, doc=False)
    d._before, d._after = img, img.scaled(120, 80)
    studio.settings.set("preview_mode", 9)
    assert d._view_mode() == 9                                                # gmic resizes both to the larger one
    studio.settings.set("preview_mode", 2)
    assert d._view_mode() == 0                                                # the natively drawn modes still need equal sizes


def test_a_split_the_filter_draws_itself_can_be_dragged_and_renders_again(app, studio):
    from gmic_studio.compat import QtGui
    d = _make_docker(app, studio, doc=False)
    fmt = QtGui.QImage.Format.Format_RGBA8888 if hasattr(QtGui.QImage, "Format") else QtGui.QImage.Format_RGBA8888
    img = QtGui.QImage(60, 40, fmt)
    img.fill(QtGui.QColor(0, 0, 255))
    d.fdef = studio.index.get("Basics > Blur")
    runs = []
    d._run_preview = lambda *a, **k: runs.append(1)
    studio.embedded_split_mode = 9                                             # the preview drew a Duplicate Horizontal split
    d._before = d._after = img
    d.preview.show_image(img)
    d._refresh_view()
    assert d.preview._mode == 9 and d.preview._before is None                  # only the divider is ours
    d.preview.splitMoved.emit(50.0, 20.0)
    assert studio.split_override == (50.0, 20.0)
    assert studio.settings.get("preview_split") != [50.0, 20.0]                # the Qt-side modes keep their own position
    d.preview.splitReleased.emit()
    assert runs == [1]                                                         # released: render again with the divider moved
    studio.embedded_split_mode = None
    d._refresh_view()
    assert d.preview._mode == 0
    d.preview.splitReleased.emit()                                             # a normal split: no new render
    assert runs == [1]


def test_split_modes_show_the_original_in_the_right_places(app):
    from gmic_studio.compat import QtCore, QtGui
    from gmic_studio.preview_view import MODES, PreviewView, original_regions
    assert len(MODES) == 13 and [n for _l, n in MODES] == list(range(13))
    assert original_regions(2, 30, 70) == [(0, 0, 0.3, 1)]                    # forward vertical: left part
    assert original_regions(4, 30, 70) == [(0.3, 0, 1, 1)]
    assert original_regions(1, 30, 70) == [(0, 0, 1, 0.7)] and original_regions(3, 30, 70) == [(0, 0.7, 1, 1)]
    assert len(original_regions(11, 50, 50)) == 2 and original_regions(11, 50, 50) != original_regions(12, 50, 50)
    assert original_regions(0, 50, 50) == [] and original_regions(7, 50, 50) == []
    fmt = QtGui.QImage.Format.Format_RGBA8888 if hasattr(QtGui.QImage, "Format") else QtGui.QImage.Format_RGBA8888
    before, after = QtGui.QImage(100, 100, fmt), QtGui.QImage(100, 100, fmt)
    before.fill(QtGui.QColor(255, 0, 0))
    after.fill(QtGui.QColor(0, 0, 255))
    v = PreviewView()
    v.resize(100, 100)
    v.show_image(after)
    v.fit()
    v.set_compare(before, 2, (50.0, 50.0))
    shot = v.grab().toImage()
    assert shot.pixelColor(10, 50).red() > 200 and shot.pixelColor(90, 50).blue() > 200   # left original, right filtered
    moved = []
    v.splitMoved.connect(lambda x, y: moved.append((round(x), round(y))))
    c = v._split_pos()
    _click(v, "press", c.x(), c.y())
    to = QtCore.QPointF(v._off.x() + v._zoom * 25, v._off.y() + v._zoom * 50)
    _click(v, "move", to.x(), to.y())
    _click(v, "release", to.x(), to.y())
    assert moved and moved[-1] == (25, 50)
    v.set_compare(None)
    assert not v.comparing


def test_docker_preview_mode_menu_and_modes_are_remembered(app, studio):
    from gmic_studio.compat import QtGui
    d = _make_docker(app, studio, doc=False)
    fmt = QtGui.QImage.Format.Format_RGBA8888 if hasattr(QtGui.QImage, "Format") else QtGui.QImage.Format_RGBA8888
    a, b = QtGui.QImage(40, 30, fmt), QtGui.QImage(40, 30, fmt)
    a.fill(QtGui.QColor(255, 0, 0))
    b.fill(QtGui.QColor(0, 0, 255))
    d._on_preview(a, b)
    assert not d.preview.comparing
    d._mode_actions[2].trigger()
    assert studio.settings.get("preview_mode") == 2 and d.preview.comparing
    d._mode_actions[0].trigger()
    assert not d.preview.comparing
    d._set_view_mode(1)
    d._on_preview(a, b.scaled(20, 15))                                        # sizes differ: split modes cannot line up
    assert not d.preview.comparing


@_NEEDS_GMIC
def test_duplicate_modes_come_from_gmic_with_the_real_filtered_image(app, studio):
    from gmic_studio.compat import QtGui
    ready(app, studio)
    from gmic_studio.core import engine as engine_mod
    studio.exe = engine_mod.locate()                                            # the real one, not the test double
    fmt = QtGui.QImage.Format.Format_RGBA8888 if hasattr(QtGui.QImage, "Format") else QtGui.QImage.Format_RGBA8888
    before, after = QtGui.QImage(400, 200, fmt), QtGui.QImage(400, 200, fmt)
    before.fill(QtGui.QColor(255, 0, 0))
    after.fill(QtGui.QColor(0, 0, 255))
    got = []
    studio.render_split(before, after, 5, 50.0, 50.0, got.append)             # duplicate top
    wait(app, lambda: got)
    out = got[0]
    assert out is not None and out.width() >= 400
    top, bottom = out.pixelColor(out.width() // 2, 5), out.pixelColor(out.width() // 2, out.height() - 6)
    assert top.red() > 200 and top.blue() < 60 and bottom.blue() > 200 and bottom.red() < 60   # original above, filtered below


def test_keep_original_size_cuts_a_bigger_result_around_its_center_and_the_preview_frames_it(app, studio):
    from gmic_studio.compat import QtGui
    ready(app, studio)
    fmt = QtGui.QImage.Format.Format_RGBA8888 if hasattr(QtGui.QImage, "Format") else QtGui.QImage.Format_RGBA8888
    big = QtGui.QImage(200, 120, fmt)
    big.fill(QtGui.QColor(0, 0, 255))
    p = QtGui.QPainter(big)
    p.fillRect(80, 50, 40, 20, QtGui.QColor(255, 0, 0))                       # the middle of the big picture
    p.end()
    assert studio._keep_size(big, 40, 20).size() == big.size()                 # off: untouched
    studio.settings.set("keep_size", True)
    cut = studio._keep_size(big, 40, 20)
    assert (cut.width(), cut.height()) == (40, 20)
    assert cut.pixelColor(0, 0).red() > 200 and cut.pixelColor(39, 19).red() > 200   # exactly the middle part
    grown = studio._keep_size(QtGui.QImage(10, 10, fmt), 40, 20)               # a smaller result is padded
    assert (grown.width(), grown.height()) == (40, 20)
    from gmic_studio.preview_view import PreviewView
    v = PreviewView()
    v.resize(200, 120)
    v.show_image(big)
    v.fit()
    v.set_frame((100, 60))
    shot = v.grab().toImage()
    at = lambda ix, iy: shot.pixelColor(int(v._off.x() + v._zoom * ix), int(v._off.y() + v._zoom * iy))
    inside, outside = at(60, 30), at(5, 5)                                     # image pixels, inside / outside the 100x50 frame
    assert inside.blue() > outside.blue() + 40                                 # outside the frame is dimmed
    d = _make_docker(app, studio, doc=False)
    d._on_preview(QtGui.QImage(100, 50, fmt), big)
    assert d.preview._frame == (100, 50)
    d._on_preview(big, big)
    assert d.preview._frame is None


def test_everyday_options_are_on_the_docker_not_in_the_menu(app, studio):
    from gmic_studio.compat import QtCore, QtGui
    d = _make_docker(app, studio, doc=False)
    d.resize(360, 900)
    d.show()
    for _ in range(5):
        app.processEvents()
    y = lambda w: w.mapTo(d.widget(), QtCore.QPoint(0, 0)).y()
    assert d.mode_btn.text().startswith("Full") and d.mode_btn.isVisible() and not d.crop_btn.isVisible()   # crop shows only when it matters
    assert abs(y(d.mode_btn) - y(d.live)) < 6 and abs(y(d.dice) - y(d.live)) < 6 and y(d.live) < y(d.preview)               # all in the controls row above the preview
    seed = studio.seed
    d.dice.click()
    assert studio.seed != seed                                                   # the dice re-rolls the seed
    d._mode_actions[2].trigger()
    assert d.mode_btn.text().startswith("Forward V")
    fmt = QtGui.QImage.Format.Format_RGBA8888 if hasattr(QtGui.QImage, "Format") else QtGui.QImage.Format_RGBA8888
    a, b = QtGui.QImage(40, 30, fmt), QtGui.QImage(80, 60, fmt)
    d._on_preview(a, b)                                                          # the filter changed the size
    app.processEvents()
    assert d.crop_btn.isVisible() and abs(y(d.crop_btn) - y(d.live)) < 6
    d.crop_btn.click()
    assert studio.settings.get("keep_size") is True and d.keep_size.isChecked()
