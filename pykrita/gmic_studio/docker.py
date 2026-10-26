"""The G'MIC Studio docker: find a filter or preset, tweak it, see it, apply it."""
import os
import time

from krita import DockWidget

from .compat import QtCore, QtGui, QtWidgets, enum
from . import krita_bridge as bridge
from .core import library
from .elided_label import ElidedLabel
from .icons import gear_icon
from .list_widgets import ActionList, batch_buttons
from .param_form import ParamForm
from .preset_ui import PresetSaveDialog
from .preview_view import DUPLICATE_MODES, MODES, PreviewView
from .recent_search import RecentButton, SearchEdit
from .settings_dialog import SettingsDialog
from .studio import get_studio

W = QtWidgets
_ID_ROLE = enum(QtCore.Qt, "ItemDataRole", "UserRole")
_ENABLED = enum(QtCore.Qt, "ItemFlag", "ItemIsEnabled")
_STAR, _NO_STAR = "★", "☆"
APPLY_MODES = (
    ("New layer", "new_layer", "The result goes on a new layer above; the original stays untouched.\nCtrl+Z removes the new layer."),
    ("New layer, hide original", "hide", "Like \"New layer\", then the original is hidden.\nCtrl+Z twice undoes both."),
    ("Replace original", "replace", "The result takes the original's place and the original is removed.\nCtrl+Z twice brings it back."),
)
_PLACE = {"library": "your library", "document": "this document"}
FILTERS_TAB, FAVORITES_TAB, PRESETS_TAB = 0, 1, 2
GROUP_PREFIX = "group:"                       # item keys of category nodes (leaves carry a filter id)
DEFAULT_OPEN = set()


_QAction = QtGui.QAction if hasattr(QtGui, "QAction") else QtWidgets.QAction


def _compact(button, text, tip):
    """Small button that does not widen the docker."""
    button.setText(text)
    button.setToolTip(tip)
    return button


class StudioDocker(DockWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("G'MIC Studio")
        self.studio = get_studio()
        self.fdef = None
        self.preset_id = None
        self._before = self._after = None
        self._preview_busy = False
        self._last_sig = None
        self._expanded = set(DEFAULT_OPEN)       # category nodes the user has open
        self._tree_scroll = 0
        self._tree_grouped = False               # the tree currently shows categories (not search results)
        self._sel_parent = ""                    # category the selected filter was picked from
        self._pv_started = 0.0
        self._pv_text = ""

        root = W.QWidget()
        root.setFocusPolicy(enum(QtCore.Qt, "FocusPolicy", "ClickFocus"))     # clicking empty space leaves the search box
        self.setWidget(root)
        lay = W.QVBoxLayout(root)
        lay.setSpacing(3)
        lay.setContentsMargins(4, 4, 4, 4)

        # shown only when gmic is missing; otherwise the gear's tooltip tells the version
        self.banner = W.QLabel("")
        self.banner.setWordWrap(True)
        lay.addWidget(self.banner)

        # one search box for every list, the recent filters, and the settings
        find = W.QHBoxLayout()
        find.setSpacing(3)
        self.search = SearchEdit()
        self.search.setPlaceholderText("Search filters and presets...")
        self.search.setClearButtonEnabled(True)
        self.recent = RecentButton()
        self.gear = W.QToolButton()
        self.gear.setToolTip("Settings")
        self.gear.setIcon(gear_icon(self.gear.palette().buttonText().color()))
        self.gear.setIconSize(QtCore.QSize(16, 16))
        self.gear.setFixedSize(self.recent.sizeHint().height(), self.recent.sizeHint().height())
        find.addWidget(self.search, 1)
        find.addWidget(self.recent)
        find.addWidget(self.gear)
        lay.addLayout(find)

        # Filters | Favorites | Presets: three views of the same library
        self.tabs = W.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setElideMode(enum(QtCore.Qt, "TextElideMode", "ElideRight"))
        self.tabs.setUsesScrollButtons(False)
        self.tree = W.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setContextMenuPolicy(enum(QtCore.Qt, "ContextMenuPolicy", "CustomContextMenu"))
        self.tabs.addTab(self.tree, "Filters")

        self.flist = ActionList()
        self.flist.setToolTip("Click to load and preview. Shift/Ctrl-click selects several; Delete removes them.")
        fpage = W.QWidget()
        fl = W.QVBoxLayout(fpage)
        fl.setContentsMargins(0, 2, 0, 0)
        fl.addWidget(self.flist, 1)
        row, self.fav_all_btn, self.fav_remove_btn = batch_buttons(self.flist, "Remove from favorites")
        fl.addLayout(row)
        self.tabs.addTab(fpage, "Favorites")

        self.plist = ActionList()
        self.plist.setToolTip("Click to load and preview, double-click to apply. Shift/Ctrl-click selects several; "
                              "Delete removes them. Right-click for more.")
        ppage = W.QWidget()
        pl = W.QVBoxLayout(ppage)
        pl.setContentsMargins(0, 2, 0, 0)
        self.only_this = W.QCheckBox("This filter only")
        self.only_this.setToolTip("Untick to see the presets of every filter in one list")
        self.scope_box = W.QComboBox()
        self.scope_box.addItem("All", None)
        self.scope_box.addItem("My library", "library")
        self.scope_box.addItem("This document", "document")
        self.scope_box.setToolTip("Where the presets come from: your library file, or the open Krita document")
        self.scope_box.setSizeAdjustPolicy(enum(W.QComboBox, "SizeAdjustPolicy", "AdjustToMinimumContentsLengthWithIcon"))
        self.scope_box.setMinimumContentsLength(6)
        prow = W.QHBoxLayout()
        prow.addWidget(self.only_this, 1)
        prow.addWidget(self.scope_box)
        pl.addLayout(prow)
        pl.addWidget(self.plist, 1)
        row, self.preset_all_btn, self.preset_delete_btn = batch_buttons(self.plist, "Delete selected")
        pl.addLayout(row)
        self.tabs.addTab(ppage, "Presets")
        self.tabs.setMinimumHeight(130)
        lay.addWidget(self.tabs, 3)

        # selected filter: name, favorite, reset, randomize
        head = W.QHBoxLayout()
        self.title = W.QLabel("<b>Pick a filter</b>")
        self.title.setSizePolicy(enum(W.QSizePolicy, "Policy", "Ignored"), enum(W.QSizePolicy, "Policy", "Preferred"))
        self.fav = _compact(W.QToolButton(), _NO_STAR, "Add to favorites")
        self.reset = _compact(W.QToolButton(), "Reset", "Back to the filter's default values")
        self.rand_btn = W.QToolButton()
        self.rand_btn.setText("Randomize")
        self.rand_btn.setToolTip("Give every unlocked value a random one (and a new random seed).\n"
                                 "Double-click a parameter's name to lock it first. The arrow has gentler options.")
        self.rand_btn.setPopupMode(enum(W.QToolButton, "ToolButtonPopupMode", "MenuButtonPopup"))
        self.rand_menu = W.QMenu(self.rand_btn)
        self.rand_btn.setMenu(self.rand_menu)
        head.addWidget(self.title, 1)
        for b in (self.fav, self.reset, self.rand_btn):
            head.addWidget(b)
        lay.addLayout(head)

        # parameters
        self.form = ParamForm()
        scroll = W.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form)
        scroll.setMinimumHeight(100)
        lay.addWidget(scroll, 3)

        # presets: one row, one menu
        pre = W.QHBoxLayout()
        self.save_btn = W.QPushButton("Save preset...")
        self.update_btn = W.QPushButton("Update")
        self.update_btn.setToolTip("Replace the loaded preset with the current values")
        self.more = _compact(W.QToolButton(), "...", "Manage presets")
        self.more.setPopupMode(enum(W.QToolButton, "ToolButtonPopupMode", "InstantPopup"))
        self.more_menu = W.QMenu(self.more)
        self.more.setMenu(self.more_menu)
        pre.addWidget(self.save_btn, 1)
        pre.addWidget(self.update_btn)
        pre.addWidget(self.more)
        lay.addLayout(pre)

        # preview controls: one row; the less used options live in the menu at its end
        opts = W.QHBoxLayout()
        opts.setSpacing(3)
        self.live = W.QCheckBox("Live")
        self.live.setToolTip("Update the preview as you change values")
        self.live.setChecked(bool(self.studio.settings.get("live_preview")))
        refresh = _compact(W.QToolButton(), "↻", "Refresh: render the preview again at full size")
        self.compare = W.QPushButton("Compare")
        self.compare.setToolTip("Hold to see the original")
        fit = _compact(W.QToolButton(), "Fit", "Fit the preview to the panel")
        one = _compact(W.QToolButton(), "100%", "Actual size")
        self.opts_btn = _compact(W.QToolButton(), "⋯", "Preview options")
        self.opts_btn.setPopupMode(enum(W.QToolButton, "ToolButtonPopupMode", "InstantPopup"))
        self.opts_menu = W.QMenu(self.opts_btn)
        self.opts_btn.setMenu(self.opts_menu)
        self.tbg = _QAction("Force transparent background", self)
        self.tbg.setCheckable(True)
        self.tbg.setToolTip("Make the fill a filter paints over empty areas transparent, whatever the filter does.\n"
                            "Off by default: many filters have their own option for this (look for a result type or\n"
                            "background setting in their parameters), which is usually the better choice.")
        self.tbg.setChecked(bool(self.studio.settings.get("transparent_bg")))
        self.watch_act = _QAction("Refresh when the layer changes", self)
        self.watch_act.setCheckable(True)
        self.watch_act.setChecked(bool(self.studio.settings.get("watch_layer")))
        self.keep_size = _QAction("Keep the original size", self)
        self.keep_size.setCheckable(True)
        self.keep_size.setToolTip("Some filters change the picture's size (3D views with their own Width/Height, borders...).\n"
                                  "On: the result is cut to the original size around its center, so you get what the preview\n"
                                  "frame shows. Off: the result keeps its full size and is added as a larger layer.")
        self.keep_size.setChecked(bool(self.studio.settings.get("keep_size")))
        self.keep_size.toggled.connect(self._keep_size_toggled)
        for act in (self.tbg, self.watch_act):
            self.opts_menu.addAction(act)
        view_menu = W.QMenu(self)                          # shown by the mode button on the preview
        group = (QtGui.QActionGroup if hasattr(QtGui, "QActionGroup") else QtWidgets.QActionGroup)(self)
        self._mode_actions = {}
        for label, number in MODES:
            act = _QAction(label, self)
            act.setCheckable(True)
            act.setActionGroup(group)
            act.triggered.connect(lambda _c=False, n=number: self._set_view_mode(n))
            view_menu.addAction(act)
            self._mode_actions[number] = act
        self._mode_actions.get(int(self.studio.settings.get("preview_mode")), self._mode_actions[0]).setChecked(True)
        self._split_token = 0
        opts.addWidget(self.live)
        opts.addWidget(refresh)
        # options used all the time sit in this row: how to look at the result, a new seed, the canvas crop
        self.mode_btn = _compact(W.QToolButton(), "Full", "Preview mode: split, checkered and duplicate views of original and result")
        self.mode_btn.setPopupMode(enum(W.QToolButton, "ToolButtonPopupMode", "InstantPopup"))
        self.mode_btn.setMenu(view_menu)
        self.dice = _compact(W.QToolButton(), "\U0001F3B2", "New random seed: random filters repeat the same result until you re-roll")
        self.dice.clicked.connect(lambda _c=False: self._reroll())
        self.crop_btn = _compact(W.QToolButton(), "Crop", self.keep_size.toolTip())
        self.crop_btn.setCheckable(True)
        self.crop_btn.setChecked(self.keep_size.isChecked())
        self.crop_btn.toggled.connect(self.keep_size.setChecked)
        self.keep_size.toggled.connect(self.crop_btn.setChecked)
        self.crop_btn.hide()
        # the row may shrink (a button's text is clipped) rather than widen the docker
        for w, width in ((self.compare, 50), (self.mode_btn, 34), (self.dice, 26), (self.crop_btn, 34),
                         (refresh, 24), (fit, 28), (one, 38), (self.opts_btn, 22)):
            w.setMinimumWidth(width)
        for w in (self.mode_btn, self.dice, self.crop_btn):
            opts.addWidget(w)
        opts.addWidget(self.compare)
        opts.addStretch(1)
        opts.addWidget(fit)
        opts.addWidget(one)
        opts.addWidget(self.opts_btn)
        lay.addLayout(opts)

        self.preview = PreviewView()
        lay.addWidget(self.preview, 3)
        self._sync_mode_button()

        # status (one line; the busy indicator is drawn on the preview itself) + apply
        self.status = ElidedLabel()
        lay.addWidget(self.status)
        row = W.QHBoxLayout()
        self.mode = W.QComboBox()
        self.mode.setSizeAdjustPolicy(enum(W.QComboBox, "SizeAdjustPolicy", "AdjustToMinimumContentsLengthWithIcon"))
        self.mode.setMinimumContentsLength(6)
        for label, key, tip in APPLY_MODES:
            self.mode.addItem(label, key)
            self.mode.setItemData(self.mode.count() - 1, tip, enum(QtCore.Qt, "ItemDataRole", "ToolTipRole"))
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.studio.settings.get("result_mode"))))
        self.mode.setToolTip("What Apply does with the result (hover an entry for details)")
        self.apply_btn = W.QPushButton("Apply")
        self.cancel_btn = W.QPushButton("Stop")
        self.cancel_btn.setEnabled(False)
        row.addWidget(self.mode)
        row.addWidget(self.apply_btn, 1)
        row.addWidget(self.cancel_btn)
        lay.addLayout(row)

        # debounce timer for live preview
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._run_preview)
        self._pv_timer = QtCore.QTimer(self)                 # shows how long a preview has been rendering
        self._pv_timer.setInterval(1000)
        self._pv_timer.timeout.connect(self._pv_tick)
        # Krita does not tell plugins when the layer, selection or pixels change, so look now and then
        self.watch = QtCore.QTimer(self)
        self.watch.setInterval(1200)
        self.watch.timeout.connect(self._watch_tick)
        self.watch.start()

        # wiring
        self.search.textChanged.connect(lambda _t: self._refresh_lists())
        self.recent.recentChosen.connect(self._open_recent)
        self.recent.clearRequested.connect(self._clear_recents)
        self.search.returnPressed.connect(self._focus_results)
        self.only_this.toggled.connect(lambda _on: self._fill_presets())
        self.scope_box.currentIndexChanged.connect(lambda _i: self._fill_presets())
        self.tree.currentItemChanged.connect(self._on_tree_item)
        self.plist.currentItemChanged.connect(self._on_list_item)
        self.flist.currentItemChanged.connect(self._on_fav_item)
        self.plist.itemDoubleClicked.connect(lambda _item: self._apply())
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.plist.customContextMenuRequested.connect(self._list_menu)
        self.flist.customContextMenuRequested.connect(self._fav_menu)
        self.plist.deleteRequested.connect(self._delete_selected_presets)
        self.preset_delete_btn.clicked.connect(self._delete_selected_presets)
        self.flist.deleteRequested.connect(self._remove_selected_favorites)
        self.fav_remove_btn.clicked.connect(self._remove_selected_favorites)
        self.rand_btn.clicked.connect(lambda: self._randomize(1.0))
        self.rand_menu.aboutToShow.connect(self._build_rand_menu)
        self.form.changed.connect(self._on_params_changed)
        self.form.changed.connect(self._refresh_handles)
        self.preview.handleMoved.connect(self.form.set_point)
        self.preview.dragFinished.connect(self._drag_finished)
        self.preview.splitMoved.connect(self._split_moved)
        self.preview.splitReleased.connect(self._split_released)
        self.form.scrubFinished.connect(self._drag_finished)
        self.form.buttonPressed.connect(lambda _i: self._run_preview())
        self.fav.clicked.connect(self._toggle_favorite)
        self.reset.clicked.connect(self.form.reset)
        self.save_btn.clicked.connect(self._save_preset)
        self.update_btn.clicked.connect(self._update_current_preset)
        self.more_menu.aboutToShow.connect(self._build_more_menu)
        self.live.toggled.connect(self._live_toggled)
        self.tbg.toggled.connect(self._bg_toggled)
        refresh.clicked.connect(lambda: self._run_preview(force_full=True))
        fit.clicked.connect(self.preview.fit)
        fit.clicked.connect(self.preview.update)
        one.clicked.connect(lambda: self.preview.actual_size())
        self.compare.pressed.connect(self._hold_compare)
        self.compare.released.connect(self._refresh_view)
        self.mode.currentIndexChanged.connect(lambda _i: self.studio.settings.set("result_mode", self.mode.currentData()))
        self.apply_btn.clicked.connect(self._apply)
        self.cancel_btn.clicked.connect(self.studio.cancel_apply)
        self.gear.clicked.connect(self._open_settings)
        self.watch_act.toggled.connect(lambda on: self.studio.settings.set("watch_layer", bool(on)))

        st = self.studio
        st.engineChanged.connect(self._update_banner)
        st.indexChanged.connect(self._refresh_lists)
        st.presetsChanged.connect(self._refresh_lists)
        st.busyChanged.connect(self._on_busy)
        st.previewBusy.connect(self._on_preview_busy)
        st.status.connect(self._on_status)
        st.reported.connect(self._on_reported)
        st.previewReady.connect(self._on_preview)
        self._update_banner()
        self.studio.sync_document()
        self._refresh_lists()
        self._sync_buttons()
        if not st.exe:
            st.refresh()

    # DockWidget abstract hook
    def canvasChanged(self, canvas):
        pass

    # ---- the two lists (one source of truth: studio.index + studio.presets) ---------------
    @staticmethod
    def _key(item):
        return item.data(0, _ID_ROLE) if item is not None else None

    def _query(self):
        return self.search.text().strip()

    def _refresh_lists(self, *_ignored):
        self._fill_tree()
        self._fill_favorites()
        self._fill_presets()
        self._fill_recents()
        self._pick_useful_tab()

    def _fill_recents(self):
        idx = self.studio.index
        found = [idx.get(i) for i in self.studio.settings.get("recents") if idx.get(i)]
        self.recent.set_recents([("%s  ·  %s" % (f.name, " > ".join(f.path) or "Filters"), f.id) for f in found])

    def _focus_results(self):
        """Enter in the search box: move to the list, so the arrow keys step through the matches."""
        view = {FILTERS_TAB: self.tree, FAVORITES_TAB: self.flist, PRESETS_TAB: self.plist}[self.tabs.currentIndex()]
        view.setFocus()
        if view is self.tree and self.tree.currentItem() is None and self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        elif view is not self.tree and view.currentRow() < 0 and view.count():
            view.setCurrentRow(0)

    def _clear_recents(self):
        self.studio.settings.set("recents", [])
        self._fill_recents()
        self._on_status("Cleared the recent filters.", False)

    def _open_recent(self, fid):
        """Jump to a recently used filter: show it in its category and load it."""
        self.tabs.setCurrentIndex(FILTERS_TAB)
        if self._query():
            self.search.clear()
        self._select_in_tree(fid, expand=True, scroll=True)

    def _capture_tree_state(self):
        """Remember which categories are open and how far the list is scrolled, before rebuilding it."""
        if not self._tree_grouped:
            return
        opened, stack = set(), [self.tree.invisibleRootItem()]
        while stack:
            node = stack.pop()
            for i in range(node.childCount()):
                child = node.child(i)
                if child.childCount():
                    if child.isExpanded():
                        opened.add(self._key(child))
                    stack.append(child)
        self._expanded = opened
        self._tree_scroll = self.tree.verticalScrollBar().value()

    def _add_group(self, parent, label, key):
        node = W.QTreeWidgetItem([label])
        node.setData(0, _ID_ROLE, key)
        parent.addChild(node)
        node.setExpanded(key in self._expanded)
        return node

    def _fill_tree(self):
        tree, idx, st = self.tree, self.studio.index, self.studio.settings
        keep = self.fdef.id if self.fdef and self.preset_id is None else None
        self._capture_tree_state()
        tree.blockSignals(True)
        tree.clear()
        query = self._query()
        self._tree_grouped = not query
        if query:
            for f in idx.search(query):
                self._add_leaf(tree.invisibleRootItem(), f, show_path=True)
        else:
            groups = {}
            for f in sorted(idx.filters, key=lambda f: (f.path, f.name.lower())):
                parent = tree.invisibleRootItem()
                for depth, part in enumerate(f.path):
                    key = f.path[:depth + 1]
                    if key not in groups:
                        groups[key] = self._add_group(parent, part, GROUP_PREFIX + " > ".join(key))
                    parent = groups[key]
                self._add_leaf(parent, f)
        if keep:
            self._select_in_tree(keep, prefer=self._sel_parent)
        tree.blockSignals(False)
        if self._tree_grouped:
            bar = self.tree.verticalScrollBar()
            bar.setValue(self._tree_scroll)
            QtCore.QTimer.singleShot(0, lambda: bar.setValue(self._tree_scroll))   # once the layout knows its size

    def _add_leaf(self, parent, f, show_path=False):
        label = f.name + ("   (%s)" % " > ".join(f.path) if show_path and f.path else "")
        item = W.QTreeWidgetItem([label])
        item.setData(0, _ID_ROLE, f.id)
        parent.addChild(item)

    def _fill_favorites(self):
        lst, idx = self.flist, self.studio.index
        shown = library.list_favorites(idx, self.studio.settings.get("favorites"), self._query())
        keep = self.fdef.id if self.fdef and self.preset_id is None else None
        scroll = lst.verticalScrollBar().value()
        lst.blockSignals(True)
        lst.clear()
        for f in shown:
            item = W.QListWidgetItem("%s  ·  %s" % (f.name, " > ".join(f.path) or "Filters"))
            item.setData(_ID_ROLE, f.id)
            lst.addItem(item)
        if keep:
            self._select_in_list(lst, keep)
        lst.blockSignals(False)
        lst.verticalScrollBar().setValue(scroll)
        lst.itemSelectionChanged.emit()                  # keeps the "Remove (n)" button in step with the new contents
        self.tabs.setTabText(FAVORITES_TAB, "Favorites (%d)" % len(shown))

    def _fill_presets(self):
        lst, store, idx = self.plist, self.studio.presets, self.studio.index
        only = self.fdef.id if self.only_this.isChecked() and self.fdef else None
        if self.preset_id and store.get(self.preset_id) is None:        # it lived in a document that is gone
            self.preset_id = None
            self._sync_buttons()
        shown = library.list_presets(store, idx, self._query(), only, self.scope_box.currentData())
        scroll = lst.verticalScrollBar().value()
        lst.blockSignals(True)
        lst.clear()
        for p in shown:
            fdef = idx.get(p["filter"])
            in_doc = p["scope"] == "document"
            item = W.QListWidgetItem(library.display_name(p, idx) + ("   [document]" if in_doc else "")
                                     + ("" if fdef else "   (filter not available)"))
            item.setData(_ID_ROLE, p["id"])
            item.setToolTip("%s%s%s" % (p["filter"], "\nStored inside this Krita document" if in_doc else "",
                                        "\nIncludes a saved random seed" if p["seed"] is not None else ""))
            if fdef is None:
                item.setFlags(item.flags() & ~_ENABLED)
            lst.addItem(item)
        if self.preset_id:
            self._select_in_list(lst, self.preset_id)
        lst.blockSignals(False)
        lst.verticalScrollBar().setValue(scroll)
        lst.itemSelectionChanged.emit()                  # keeps the "Delete selected (n)" button in step
        self.tabs.setTabText(PRESETS_TAB, "Presets (%d)" % len(shown))

    def _tab_counts(self):
        return {FILTERS_TAB: self.tree.invisibleRootItem().childCount(), FAVORITES_TAB: self.flist.count(),
                PRESETS_TAB: self.plist.count()}

    def _pick_useful_tab(self):
        """While searching, jump to a tab that has results if the current one has none."""
        if not self._query():
            return
        counts = self._tab_counts()
        if counts[self.tabs.currentIndex()] == 0:
            for tab in (FILTERS_TAB, PRESETS_TAB, FAVORITES_TAB):
                if counts[tab] > 0:
                    self.tabs.setCurrentIndex(tab)
                    return

    def _select_in_tree(self, fid, expand=False, scroll=False, prefer=None):
        """Highlight `fid`. A filter can appear under Favorites, Recent and its category: take the copy in the
        category named by `prefer` if there is one. Only moves the view when `scroll` is set."""
        matches, stack = [], [self.tree.invisibleRootItem()]
        while stack:
            node = stack.pop(0)
            for i in range(node.childCount()):
                child = node.child(i)
                if self._key(child) == fid:
                    matches.append(child)
                stack.append(child)
        if not matches:
            return False
        chosen = next((m for m in matches if prefer is not None and self._parent_key(m) == prefer), matches[0])
        if expand:
            p = chosen.parent()
            while p is not None:
                p.setExpanded(True)
                p = p.parent()
        self.tree.setCurrentItem(chosen)
        if scroll:
            self.tree.scrollToItem(chosen)
        return True

    def _parent_key(self, item):
        parent = item.parent()
        return self._key(parent) if parent is not None else ""

    def _select_in_list(self, listw, key):
        for i in range(listw.count()):
            if listw.item(i).data(_ID_ROLE) == key:
                listw.setCurrentRow(i)
                return True
        return False

    def _tree_leaf_keys(self):
        """Filter ids in the order the tree shows them, without repeats (Favorites/Recent duplicate some)."""
        keys = []

        def walk(node):
            for i in range(node.childCount()):
                child = node.child(i)
                if child.childCount():
                    walk(child)
                elif self._key(child) not in keys:
                    keys.append(self._key(child))

        walk(self.tree.invisibleRootItem())
        return keys

    # ---- selecting ------------------------------------------------------------------------
    def _on_tree_item(self, item, _prev):
        fdef = self.studio.index.get(self._key(item)) if item else None
        if fdef is not None:
            self._sel_parent = self._parent_key(item)
            self.preset_id = None
            self._show_filter(fdef, self.studio.settings.values_for(fdef.id))

    def _on_list_item(self, item, _prev):
        pid = item.data(_ID_ROLE) if item else None
        if pid and len(self.plist.selectedItems()) <= 1:          # while picking several, do not load each one
            self._open_preset(pid)

    def _on_fav_item(self, item, _prev):
        fdef = self.studio.index.get(item.data(_ID_ROLE)) if item else None
        if fdef is not None and len(self.flist.selectedItems()) <= 1:
            self._sel_parent = ""
            self.preset_id = None
            self._show_filter(fdef, self.studio.settings.values_for(fdef.id))

    def _show_filter(self, fdef, values):
        self.fdef = fdef
        self.studio.split_override, self.studio.embedded_split_mode = None, None     # another filter, another split
        self.title.setText("<b>%s</b>" % _escape(fdef.name))
        self._refresh_fav()
        self._sync_buttons()
        if self.only_this.isChecked():
            self._fill_presets()
        self.form.set_filter(fdef, values)

    def _open_preset(self, pid):
        preset = self.studio.presets.get(pid)
        fdef = self.studio.index.get(preset["filter"]) if preset else None
        if fdef is None:
            self._on_status("That preset's filter is not available in the current filter list.", True)
            return
        self.preset_id = pid
        if preset["seed"] is not None:
            self.studio.seed = preset["seed"]
        self._show_filter(fdef, dict(preset["values"]))

    def _sync_buttons(self):
        self.save_btn.setEnabled(self.fdef is not None)
        self.update_btn.setEnabled(self.preset_id is not None)

    def _toggle_favorite(self):
        if self.fdef:
            self.studio.settings.toggle_favorite(self.fdef.id)
            self._refresh_fav()
            self._fill_favorites()

    def _remove_selected_favorites(self):
        ids = self.flist.selected_keys(_ID_ROLE)
        if ids:
            n = self.studio.settings.remove_favorites(ids)
            self._refresh_fav()
            self._fill_favorites()
            self._on_status("Removed %d favorite%s." % (n, "" if n == 1 else "s"), False)

    def _delete_selected_presets(self):
        ids = self.plist.selected_keys(_ID_ROLE)
        if not ids:
            return
        text = ("Delete the preset \"%s\"?" % self.studio.presets.get(ids[0])["name"]) if len(ids) == 1 \
            else "Delete these %d presets?" % len(ids)
        if self._confirm(text):
            if self.preset_id in ids:
                self.preset_id = None
                self._sync_buttons()
            n = self.studio.delete_presets(ids)
            self._on_status("Deleted %d preset%s." % (n, "" if n == 1 else "s"), False)

    def _refresh_fav(self):
        fav = self.fdef is not None and self.studio.settings.is_favorite(self.fdef.id)
        self.fav.setText(_STAR if fav else _NO_STAR)
        self.fav.setToolTip("Remove from favorites" if fav else "Add to favorites")

    # ---- presets: every menu is built by these two helpers --------------------------------
    def _copy_action(self, menu, pids):
        """'Copy to ...' for the presets in `pids`: to the open document, or (for document presets) to the library."""
        scopes = {self.studio.presets.scope_of(pid) for pid in pids} - {None}
        if len(scopes) != 1:
            return
        here = scopes.pop()
        target = "library" if here == "document" else "document"
        if target == "document" and not self.studio.presets.has_document:
            return
        n = len(pids)
        what = "my library" if target == "library" else "this document"
        label = "Copy %d presets to %s" % (n, what) if n > 1 else "Copy to %s" % what
        menu.addAction(label).triggered.connect(lambda: self._copy_presets(pids, target))

    def _copy_presets(self, pids, scope):
        try:
            n = self.studio.copy_presets(pids, scope)
        except Exception as exc:
            self._on_status("Could not copy: %s" % exc, True)
            return
        self._on_status("Copied %d preset%s to %s." % (n, "" if n == 1 else "s",
                                                       "your library" if scope == "library" else "this document"), False)

    def _preset_menu(self, menu, pid):
        self._copy_action(menu, [pid])
        menu.addAction("Rename...").triggered.connect(lambda: self._rename_preset(pid))
        menu.addAction("Export this preset...").triggered.connect(lambda: self._export([pid]))
        menu.addAction("Delete").triggered.connect(lambda: self._delete_preset(pid))

    def _library_menu(self, menu):
        menu.addAction("Export all presets...").triggered.connect(lambda: self._export(None))
        menu.addAction("Import presets...").triggered.connect(self._import)
        menu.addAction("Open the presets folder").triggered.connect(self._open_presets_folder)

    def _build_more_menu(self):
        self.more_menu.clear()
        if self.preset_id:
            self._preset_menu(self.more_menu, self.preset_id)
            self.more_menu.addSeparator()
        self._library_menu(self.more_menu)

    def _list_menu(self, pos):
        item = self.plist.itemAt(pos)
        menu = W.QMenu(self)
        many = len(self.plist.selectedItems())
        if item is not None and many > 1 and item.isSelected():
            self._copy_action(menu, self.plist.selected_keys(_ID_ROLE))
            menu.addAction("Delete %d presets" % many).triggered.connect(self._delete_selected_presets)
        elif item is not None:
            self._preset_menu(menu, item.data(_ID_ROLE))
        menu.addSeparator()
        menu.addAction("Select all").triggered.connect(self.plist.selectAll)
        self._library_menu(menu)
        menu.exec(self.plist.viewport().mapToGlobal(pos))

    def _fav_menu(self, pos):
        menu = W.QMenu(self)
        many = len(self.flist.selectedItems())
        if self.flist.itemAt(pos) is not None:
            menu.addAction("Remove %d from favorites" % many if many > 1 else "Remove from favorites").triggered.connect(
                self._remove_selected_favorites)
        menu.addAction("Select all").triggered.connect(self.flist.selectAll)
        menu.exec(self.flist.viewport().mapToGlobal(pos))

    def _tree_menu(self, pos):
        fid = self._key(self.tree.itemAt(pos))
        if self.studio.index.get(fid) is None:
            return
        menu = W.QMenu(self)
        fav = self.studio.settings.is_favorite(fid)
        menu.addAction("Remove from favorites" if fav else "Add to favorites").triggered.connect(
            lambda: self._fav_from_menu(fid))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _fav_from_menu(self, fid):
        self.studio.settings.toggle_favorite(fid)
        self._refresh_fav()
        self._fill_favorites()

    def _save_preset(self):
        if not self.fdef:
            return
        book = self.studio.presets
        current = book.get(self.preset_id) if self.preset_id else None
        wanted = current["scope"] if current else self.studio.settings.get("preset_scope")
        dlg = PresetSaveDialog(self, self.fdef.name, current["name"] if current else "", book.path,
                               book.has_document, wanted)
        if not dlg.exec():
            return
        name, scopes = dlg.name(), dlg.scopes()
        clashes = [s for s in scopes if book.find(self.fdef.id, name, s)]
        if clashes and not self._confirm("Replace the preset \"%s\" in %s?" % (name, " and ".join(_PLACE[s] for s in clashes))):
            return
        try:
            made = self.studio.save_presets(name, self.fdef, self.form.values(), dlg.lock_seed(), scopes)
        except Exception as exc:
            self._on_status("Could not save the preset: %s" % exc, True)
            return
        self.preset_id = made[0]["id"]
        self._sync_buttons()
        self._on_status("Saved preset \"%s\" in %s." % (name, " and ".join(_PLACE[s] for s in scopes)), False)

    def _update_current_preset(self):
        p = self.studio.presets.get(self.preset_id) if self.preset_id else None
        if p and self.fdef:
            try:
                self.studio.save_preset(p["name"], self.fdef, self.form.values(), p["seed"] is not None, p["scope"])
            except Exception as exc:
                self._on_status("Could not update the preset: %s" % exc, True)
                return
            self._on_status("Updated preset \"%s\"." % p["name"], False)

    def _rename_preset(self, pid):
        preset = self.studio.presets.get(pid)
        if not preset:
            return
        name, ok = W.QInputDialog.getText(self, "Rename preset", "New name:", text=preset["name"])
        if ok and name.strip() and not self.studio.rename_preset(pid, name):
            self._on_status("A preset with that name already exists for this filter.", True)

    def _delete_preset(self, pid):
        preset = self.studio.presets.get(pid)
        if preset and self._confirm("Delete the preset \"%s\"?" % preset["name"]):
            if self.preset_id == pid:
                self.preset_id = None
                self._sync_buttons()
            self.studio.delete_preset(pid)

    def _export(self, ids=None):
        path = W.QFileDialog.getSaveFileName(self, "Export presets", "gmic-studio-presets.json",
                                             "G'MIC Studio presets (*.json)")[0]
        if path:
            try:
                n = self.studio.presets.export_file(path, ids)
                self._on_status("Exported %d preset(s) to %s." % (n, path), False)
            except OSError as exc:
                self._on_status("Could not export: %s" % exc, True)

    def _import(self):
        path = W.QFileDialog.getOpenFileName(self, "Import presets", "", "G'MIC Studio presets (*.json);;All (*)")[0]
        if path:
            try:
                added, replaced = self.studio.import_presets(path)
                self._on_status("Imported %d new preset(s), replaced %d." % (added, replaced), False)
            except (OSError, ValueError) as exc:
                self._on_status("Could not import: %s" % exc, True)

    def _open_presets_folder(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(os.path.dirname(self.studio.presets.path)))

    def _confirm(self, text):
        yes, no = enum(W.QMessageBox, "StandardButton", "Yes"), enum(W.QMessageBox, "StandardButton", "No")
        return W.QMessageBox.question(self, "G'MIC Studio", text, yes | no) == yes

    # ---- randomizer -----------------------------------------------------------------------
    def _build_rand_menu(self):
        m = self.rand_menu
        m.clear()
        m.addAction("Randomize everything (new seed)").triggered.connect(lambda: self._randomize(1.0))
        m.addAction("Nudge a little (about 15%)").triggered.connect(lambda: self._randomize(0.15))
        m.addAction("Nudge more (about 35%)").triggered.connect(lambda: self._randomize(0.35))
        m.addAction("New random seed only").triggered.connect(self._reroll)
        m.addSeparator()
        n = len(self.form.locked)
        unlock = m.addAction("Unlock all values (%d locked)" % n)
        unlock.setEnabled(n > 0)
        unlock.triggered.connect(self.form.clear_locks)

    def _randomize(self, strength):
        if not self.fdef:
            return
        if strength >= 1.0:
            self.studio.new_seed()                                # random filters change too
        moved = self.form.randomize(strength)
        kept = len(self.form.locked)
        self._on_status("%s %d value%s%s." % ("Randomized" if strength >= 1.0 else "Nudged", moved, "" if moved == 1 else "s",
                                              ", kept %d locked" % kept if kept else ""), False)

    # ---- preview --------------------------------------------------------------------------
    def _live_toggled(self, on):
        self.studio.settings.set("live_preview", bool(on))
        if on:
            self._run_preview()
        else:
            self.timer.stop()
            self.studio.cancel_preview()

    def _keep_size_toggled(self, on):
        self.studio.settings.set("keep_size", bool(on))
        if self.live.isChecked():
            self._run_preview()

    def _bg_toggled(self, on):
        self.studio.settings.set("transparent_bg", bool(on))
        if self.live.isChecked():
            self._run_preview()

    def _reroll(self):
        self.studio.new_seed()
        self._run_preview()

    def _on_params_changed(self):
        if self.live.isChecked() and self.fdef:
            if self.preview.dragging or self.form.scrubbing:     # follow the handle or slider: no waiting, a small fast image
                self.timer.stop()
                self._run_preview(interactive=True)
            else:
                self.timer.start(int(self.studio.settings.get("preview_delay_ms")))

    def _on_reported(self, filter_id, status, sent):
        """A filter handed back new parameter values (a point it moved, a hidden value): show them, without a new preview."""
        if self.fdef is None or self.fdef.id != filter_id or self.preview.dragging:
            return
        if self.form.apply_status(status, sent):
            self._refresh_handles()

    def _drag_finished(self):
        """Let go of a handle or slider: one full-quality render of where it ended up."""
        if self.live.isChecked() and self.fdef:
            self._run_preview()

    def _run_preview(self, force_full=False, interactive=False):
        if self.fdef:
            self._last_sig = bridge.signature()
            values = self.form.run_values()              # includes a button that was just pressed
            self.form.release_buttons()
            self.studio.request_preview(self.fdef, values, force_full, interactive)

    def _watch_tick(self):
        """Refresh the preview by itself when the active layer, the selection or the pixels changed."""
        self.studio.sync_document()                      # embedded presets follow the document you are in
        if not (self.live.isChecked() and self.fdef and self.studio.exe and not self.studio.busy
                and self.studio.settings.get("watch_layer") and self.isVisible()):
            return
        sig = bridge.signature()
        if sig is None:
            return
        changed = self._last_sig is not None and sig != self._last_sig
        self._last_sig = sig
        if changed:
            self.timer.start(int(self.studio.settings.get("preview_delay_ms")))

    def _on_preview_busy(self, busy):
        self._preview_busy = busy
        if busy:
            self._pv_started = time.time()
            self._pv_timer.start()
        else:
            self._pv_timer.stop()
        self._sync_progress()

    def _pv_tick(self):
        """While a preview renders, show how long it has been going (so a slow filter is not mistaken for a stuck one)."""
        if self._preview_busy and self._pv_text:
            self.status.set_full_text("%s  %d s" % (self._pv_text.rstrip("."), int(time.time() - self._pv_started)))

    def _sync_progress(self):
        self.preview.set_busy(self._preview_busy or self.studio.busy)

    def _on_preview(self, before, after):
        self._before, self._after = before, after
        resized = before is not None and after is not None and before.size() != after.size()
        self.preview.set_frame((before.width(), before.height()) if resized else None)
        self.crop_btn.setVisible(resized or self.studio.last_result_resized)
        self._refresh_view()
        self._refresh_handles()

    # ---- preview modes (G'MIC-Qt's split / duplicate / checkered views) -------------------------
    def _view_mode(self):
        mode = int(self.studio.settings.get("preview_mode"))
        same = self._before is not None and self._after is not None and self._before.size() == self._after.size()
        return mode if same or mode in DUPLICATE_MODES else 0           # gmic lays the duplicates out itself, whatever the sizes

    def _sync_mode_button(self):
        number = int(self.studio.settings.get("preview_mode"))
        label = dict((n, l) for l, n in MODES).get(number, "Full")
        self.mode_btn.setText(label.split()[0] + (" " + "".join(w[0] for w in label.split()[1:]) if " " in label else "") + " \u25be")
        self.mode_btn.setToolTip("Preview mode: %s (click to change)" % label)

    def _set_view_mode(self, number):
        self.studio.settings.set("preview_mode", int(number))
        self._sync_mode_button()
        self._refresh_view()
        self._refresh_handles()

    def _hold_compare(self):
        self.preview.set_compare(None)
        self._show(self._before)

    def _embedded_split(self):
        """Mode of the before/after split the filter's own preview drew, while the plain result is shown."""
        return self.studio.embedded_split_mode if self._view_mode() == 0 else None

    def _split_moved(self, x, y):
        if self._embedded_split():
            self.studio.split_override = (x, y)
        else:
            self.studio.settings.set("preview_split", [x, y])

    def _split_released(self):
        if self._embedded_split():
            self._run_preview()                            # the filter draws it: render again with the divider moved
        else:
            self._refresh_view()

    def _refresh_view(self):
        """Show the result in the chosen preview mode."""
        if self._after is None:
            return
        mode, split = self._view_mode(), self.studio.settings.get("preview_split")
        self._split_token += 1
        if mode in DUPLICATE_MODES:
            token = self._split_token
            self._show(self._after)
            self.preview.set_compare(None, mode, split)              # only the divider; gmic composes the picture

            def ready(image):
                if image is not None and token == self._split_token and self._view_mode() == mode:
                    self._show(image)

            self.studio.render_split(self._before, self._after, mode, split[0], split[1], ready)
            return
        self._show(self._after)
        embedded = self._embedded_split()
        if embedded:
            self.preview.set_compare(None, embedded, self.studio.split_override or (50.0, 50.0))
        else:
            self.preview.set_compare(self._before, mode, split)

    def _refresh_handles(self):
        """Draggable handles for the filter's points, when the preview shows the whole image they refer to."""
        ok = (self.fdef is not None and self._after is not None and self._before is not None and self._view_mode() == 0
              and self.studio.preview_whole and self._after.size() == self._before.size())
        self.preview.set_handles(self.form.points() if ok else [])

    def _show(self, image):
        if image is not None:
            self.preview.show_image(image)

    # ---- apply / status -------------------------------------------------------------------
    def _apply(self):
        if self.fdef:
            self.timer.stop()
            if self.preset_id:
                try:
                    self.studio.presets.touch(self.preset_id)   # keeps what you use at the top of the list
                except OSError:
                    pass                                         # an unwritable presets file must not block Apply
            self.studio.apply(self.fdef, self.form.apply_values())

    def _on_busy(self, busy):
        self._sync_progress()
        self.apply_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        if not busy:
            self._refresh_lists()

    def _on_status(self, text, is_error=False):
        self._pv_text = text if text.startswith("Previewing") else ""
        self.status.set_full_text(("⚠ " if is_error else "") + text)

    def _update_banner(self):
        st = self.studio
        if st.exe:
            self.banner.hide()
            self.gear.setToolTip("Settings  (gmic %s ready)" % st.exe_version)
        else:
            self.banner.setText("gmic not found. Open Settings (the gear) to set it up.")
            self.banner.show()
            self.gear.setToolTip("Settings")
        self.apply_btn.setEnabled(bool(st.exe) and not st.busy)

    def _open_settings(self):
        SettingsDialog(self.studio, self).exec()
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.studio.settings.get("result_mode"))))
        self.tbg.setChecked(bool(self.studio.settings.get("transparent_bg")))


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
