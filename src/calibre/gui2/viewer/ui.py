#!/usr/bin/env python
# License: GPL v3 Copyright: 2018, Kovid Goyal <kovid at kovidgoyal.net>

import json
import os
import re
import sys
import time
from collections import defaultdict, namedtuple
from hashlib import sha256
from threading import Thread

from qt.core import (
    QApplication,
    QCursor,
    QDockWidget,
    QEvent,
    QMainWindow,
    QMenu,
    QMimeData,
    QModelIndex,
    QPixmap,
    Qt,
    QTimer,
    QToolBar,
    QUrl,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
    sip,
)

from calibre import prints
from calibre.constants import ismacos, iswindows
from calibre.customize.ui import available_input_formats
from calibre.db.annotations import merge_annotations
from calibre.gui2 import add_to_recent_docs, choose_files, error_dialog, sanitize_env_vars
from calibre.gui2.dialogs.drm_error import DRMErrorMessage
from calibre.gui2.image_popup import ImagePopup
from calibre.gui2.main_window import MainWindow
from calibre.gui2.viewer import get_boss, get_current_book_data, performance_monitor
from calibre.gui2.viewer.annotations import AnnotationsSaveWorker, annotations_dir, parse_annotations
from calibre.gui2.viewer.bookmarks import BookmarkManager
from calibre.gui2.viewer.config import get_session_pref, load_reading_rates, save_reading_rates, vprefs
from calibre.gui2.viewer.convert_book import prepare_book
from calibre.gui2.viewer.highlights import HighlightsPanel
from calibre.gui2.viewer.integration import get_book_library_details, load_annotations_map_from_library
from calibre.gui2.viewer.lookup import Lookup
from calibre.gui2.viewer.overlay import LoadingOverlay
from calibre.gui2.viewer.search import SearchPanel
from calibre.gui2.viewer.toc import TOC, TOCSearch, TOCView
from calibre.gui2.viewer.toolbars import ActionsToolBar
from calibre.gui2.viewer.web_view import WebView, get_path_for_name, set_book_path
from calibre.startup import connect_lambda
from calibre.utils.date import utcnow
from calibre.utils.img import image_from_path
from calibre.utils.ipc.simple_worker import WorkerError
from calibre.utils.localization import _
from polyglot.builtins import as_bytes, as_unicode, iteritems, itervalues


def is_float(x):
    try:
        float(x)
        return True
    except Exception:
        pass
    return False


def dock_defs():
    Dock = namedtuple('Dock', 'name title initial_area allowed_areas')
    ans = {}

    def d(title, name, area, allowed=Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea):
        ans[name] = Dock(name + '-dock', title, area, allowed)

    d(_('Table of Contents'), 'toc', Qt.DockWidgetArea.LeftDockWidgetArea)
    d(_('Lookup'), 'lookup', Qt.DockWidgetArea.RightDockWidgetArea)
    d(_('Bookmarks'), 'bookmarks', Qt.DockWidgetArea.RightDockWidgetArea)
    d(_('Search'), 'search', Qt.DockWidgetArea.LeftDockWidgetArea)
    d(_('Inspector'), 'inspector', Qt.DockWidgetArea.RightDockWidgetArea, Qt.DockWidgetArea.AllDockWidgetAreas)
    d(_('Highlights'), 'highlights', Qt.DockWidgetArea.RightDockWidgetArea)
    return ans


def path_key(path):
    return sha256(as_bytes(path)).hexdigest()


class EbookViewer(MainWindow):

    msg_from_anotherinstance = pyqtSignal(object)
    book_preparation_started = pyqtSignal()
    book_prepared = pyqtSignal(object, object)
    MAIN_WINDOW_STATE_VERSION = 1

    def __init__(self, open_at=None, continue_reading=None, force_reload=False, calibre_book_data=None):
        MainWindow.__init__(self, None)
        get_boss(self)
        self.annotations_saver = None
        self.calibre_book_data_for_first_book = calibre_book_data
        self.shutting_down = self.close_forced = self.shutdown_done = False
        self.force_reload = force_reload
        connect_lambda(self.book_preparation_started, self, lambda self: self.loading_overlay(_(
            'Preparing book for first read, please wait')), type=Qt.ConnectionType.QueuedConnection)
        self.maximized_at_last_fullscreen = False
        self.save_pos_timer = t = QTimer(self)
        t.setSingleShot(True), t.setInterval(3000), t.setTimerType(Qt.TimerType.VeryCoarseTimer)
        connect_lambda(t.timeout, self, lambda self: self.save_annotations(in_book_file=False))
        self.pending_open_at = open_at
        self.pending_search = None
        self.base_window_title = _('E-book viewer')
        self.setDockOptions(QMainWindow.DockOption.AnimatedDocks | QMainWindow.DockOption.AllowTabbedDocks | QMainWindow.DockOption.AllowNestedDocks)
        self.setWindowTitle(self.base_window_title)
        self.in_full_screen_mode = None
        self.image_popup = ImagePopup(self, prefs=vprefs)
        self.actions_toolbar = at = ActionsToolBar(self)
        at.open_book_at_path.connect(self.ask_for_open)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, at)
        try:
            os.makedirs(annotations_dir)
        except OSError:
            pass
        self.current_book_data = {}
        get_current_book_data(self.current_book_data)
        self.book_prepared.connect(self.load_finished, type=Qt.ConnectionType.QueuedConnection)
        self.dock_defs = dock_defs()

        def create_dock(title, name, area, areas=Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea):
            ans = QDockWidget(title, self)
            ans.setObjectName(name)
            self.addDockWidget(area, ans)
            ans.setVisible(False)
            ans.visibilityChanged.connect(self.dock_visibility_changed)
            return ans

        for dock_def in itervalues(self.dock_defs):
            setattr(self, '{}_dock'.format(dock_def.name.partition('-')[0]), create_dock(
                dock_def.title, dock_def.name, dock_def.initial_area, dock_def.allowed_areas))

        self.toc_container = w = QWidget(self)
        w.l = QVBoxLayout(w)
        self.toc = TOCView(w)
        self.toc.clicked[QModelIndex].connect(self.toc_clicked)
        self.toc.searched.connect(self.toc_searched)
        self.toc_search = TOCSearch(self.toc, parent=w)
        w.l.addWidget(self.toc), w.l.addWidget(self.toc_search), w.l.setContentsMargins(0, 0, 0, 0)
        self.toc_dock.setWidget(w)

        self.search_widget = w = SearchPanel(self)
        w.search_requested.connect(self.start_search)
        w.hide_search_panel.connect(self.search_dock.close)
        w.count_changed.connect(self.search_results_count_changed)
        w.goto_cfi.connect(self.goto_cfi)
        self.search_dock.setWidget(w)
        self.search_dock.visibilityChanged.connect(self.search_widget.visibility_changed)

        self.lookup_widget = w = Lookup(self)
        self.lookup_dock.visibilityChanged.connect(self.lookup_widget.visibility_changed)
        self.lookup_dock.setWidget(w)

        self.bookmarks_widget = w = BookmarkManager(self)
        connect_lambda(
            w.create_requested, self,
            lambda self: self.web_view.trigger_shortcut('new_bookmark'))
        w.edited.connect(self.bookmarks_edited)
        w.activated.connect(self.bookmark_activated)
        w.toggle_requested.connect(self.toggle_bookmarks)
        self.bookmarks_dock.setWidget(w)

        self.highlights_widget = w = HighlightsPanel(self)
        self.highlights_dock.setWidget(w)
        w.toggle_requested.connect(self.toggle_highlights)

        self.web_view = WebView(self)
        self.web_view.cfi_changed.connect(self.cfi_changed)
        self.web_view.reload_book.connect(self.reload_book)
        self.web_view.toggle_toc.connect(self.toggle_toc)
        self.web_view.show_search.connect(self.show_search)
        self.web_view.find_next.connect(self.search_widget.find_next_requested)
        self.search_widget.show_search_result.connect(self.show_search_result)
        self.web_view.search_result_not_found.connect(self.search_widget.search_result_not_found)
        self.web_view.search_result_discovered.connect(self.search_widget.search_result_discovered)
        self.web_view.toggle_bookmarks.connect(self.toggle_bookmarks)
        self.web_view.toggle_highlights.connect(self.toggle_highlights)
        self.web_view.new_bookmark.connect(self.bookmarks_widget.create_new_bookmark)
        self.web_view.toggle_inspector.connect(self.toggle_inspector)
        self.web_view.toggle_lookup.connect(self.toggle_lookup)
        self.web_view.quit.connect(self.quit)
        self.web_view.update_current_toc_nodes.connect(self.toc.update_current_toc_nodes)
        self.web_view.toggle_full_screen.connect(self.toggle_full_screen)
        self.web_view.ask_for_open.connect(self.ask_for_open_from_js, type=Qt.ConnectionType.QueuedConnection)
        self.web_view.selection_changed.connect(self.lookup_widget.selected_text_changed, type=Qt.ConnectionType.QueuedConnection)
        self.web_view.selection_changed.connect(self.highlights_widget.selected_text_changed, type=Qt.ConnectionType.QueuedConnection)
        self.web_view.view_image.connect(self.view_image, type=Qt.ConnectionType.QueuedConnection)
        self.web_view.copy_image.connect(self.copy_image, type=Qt.ConnectionType.QueuedConnection)
        self.web_view.show_loading_message.connect(self.show_loading_message)
        self.web_view.show_error.connect(self.show_error)
        self.web_view.print_book.connect(self.print_book, type=Qt.ConnectionType.QueuedConnection)
        self.web_view.reset_interface.connect(self.reset_interface, type=Qt.ConnectionType.QueuedConnection)
        self.web_view.quit.connect(self.quit, type=Qt.ConnectionType.QueuedConnection)
        self.web_view.shortcuts_changed.connect(self.shortcuts_changed)
        self.web_view.scrollbar_context_menu.connect(self.scrollbar_context_menu)
        self.web_view.close_prep_finished.connect(self.close_prep_finished)
        self.web_view.highlights_changed.connect(self.highlights_changed)
        self.web_view.update_reading_rates.connect(self.update_reading_rates)
        self.web_view.edit_book.connect(self.edit_book)
        self.web_view.content_file_changed.connect(self.content_file_changed)
        self.actions_toolbar.initialize(self.web_view, self.search_dock.toggleViewAction())
        at.update_action_state(False)
        self.setCentralWidget(self.web_view)
        self.loading_overlay = LoadingOverlay(self)
        self.restore_state()
        self.actions_toolbar.update_visibility()
        self.dock_visibility_changed()
        self.highlights_widget.request_highlight_action.connect(self.web_view.highlight_action)
        self.highlights_widget.web_action.connect(self.web_view.generic_action)
        self.highlights_widget.notes_edited_signal.connect(self.notes_edited)
        if continue_reading:
            self.continue_reading()
        self.setup_mouse_auto_hide()

    def shortcuts_changed(self, smap):
        rmap = defaultdict(list)
        for k, v in iteritems(smap):
            rmap[v].append(k)
        self.actions_toolbar.set_tooltips(rmap)
        self.highlights_widget.set_tooltips(rmap)

    def resizeEvent(self, ev):
        self.loading_overlay.resize(self.size())
        return MainWindow.resizeEvent(self, ev)

    def scrollbar_context_menu(self, x, y, frac):
        m = QMenu(self)
        amap = {}

        def a(text, name):
            m.addAction(text)
            amap[text] = name

        a(_('Scroll here'), 'here')
        m.addSeparator()
        a(_('Start of book'), 'start_of_book')
        a(_('End of book'), 'end_of_book')
        m.addSeparator()
        a(_('Previous section'), 'previous_section')
        a(_('Next section'), 'next_section')
        m.addSeparator()
        a(_('Start of current file'), 'start_of_file')
        a(_('End of current file'), 'end_of_file')
        m.addSeparator()
        a(_('Hide this scrollbar'), 'toggle_scrollbar')

        q = m.exec(QCursor.pos())
        if not q:
            return
        q = amap[q.text()]
        if q == 'here':
            self.web_view.goto_frac(frac)
        else:
            self.web_view.trigger_shortcut(q)

    # IPC {{{
    def handle_commandline_arg(self, arg):
        if arg:
            if os.path.isfile(arg) and os.access(arg, os.R_OK):
                self.load_ebook(arg)
            else:
                prints('Cannot read from:', arg, file=sys.stderr)

    def message_from_other_instance(self, msg):
        try:
            msg = json.loads(msg)
            path, open_at = msg
        except Exception as err:
            print('Invalid message from other instance', file=sys.stderr)
            print(err, file=sys.stderr)
            return
        self.load_ebook(path, open_at=open_at)
        self.raise_and_focus()
        self.activateWindow()
    # }}}

    # Fullscreen {{{
    def set_full_screen(self, on):
        if on:
            self.maximized_at_last_fullscreen = self.isMaximized()
            if not self.actions_toolbar.visible_in_fullscreen:
                self.actions_toolbar.setVisible(False)
            self.showFullScreen()
        else:
            self.actions_toolbar.update_visibility()
            if self.maximized_at_last_fullscreen:
                self.showMaximized()
            else:
                self.showNormal()

    def changeEvent(self, ev):
        if ev.type() == QEvent.Type.WindowStateChange:
            in_full_screen_mode = self.isFullScreen()
            if self.in_full_screen_mode is None or self.in_full_screen_mode != in_full_screen_mode:
                self.in_full_screen_mode = in_full_screen_mode
                self.web_view.notify_full_screen_state_change(self.in_full_screen_mode)
        return MainWindow.changeEvent(self, ev)

    def toggle_full_screen(self):
        self.set_full_screen(not self.isFullScreen())

    # }}}

    # Docks (ToC, Bookmarks, Lookup, etc.) {{{

    def toggle_inspector(self):
        visible = self.inspector_dock.toggleViewAction().isChecked()
        self.inspector_dock.setVisible(not visible)

    def toggle_toc(self):
        is_visible = self.toc_dock.isVisible()
        self.toc_dock.setVisible(not is_visible)
        if not is_visible:
            self.toc.scroll_to_current_toc_node()

    def show_search_result(self, sr):
        self.web_view.show_search_result(sr)

    def show_search(self, text, trigger=False, search_type=None, case_sensitive=None):
        self.search_dock.setVisible(True)
        self.search_dock.activateWindow()
        self.search_dock.raise_and_focus()
        self.search_widget.focus_input(text, search_type, case_sensitive)
        if trigger:
            self.search_widget.trigger()

    def search_results_count_changed(self, num=-1):
        if num < 0:
            tt = _('Search')
        elif num == 0:
            tt = _('Search :: no matches')
        elif num == 1:
            tt = _('Search :: one match')
        else:
            tt = _('Search :: {} matches').format(num)
        self.search_dock.setWindowTitle(tt)

    def start_search(self, search_query):
        name = self.web_view.current_content_file
        if name:
            if search_query.is_empty and search_query.text:
                return error_dialog(self, _('Empty search expression'), _(
                    'Cannot search for {!r} as it contains only punctuation and spaces.').format(search_query.text), show=True)
            self.web_view.get_current_cfi(self.search_widget.set_anchor_cfi)
            self.search_widget.start_search(search_query, name)
            self.web_view.setFocus(Qt.FocusReason.OtherFocusReason)

    def toggle_bookmarks(self):
        is_visible = self.bookmarks_dock.isVisible()
        self.bookmarks_dock.setVisible(not is_visible)
        if is_visible:
            self.web_view.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.bookmarks_widget.bookmarks_list.setFocus(Qt.FocusReason.OtherFocusReason)

    def toggle_highlights(self):
        is_visible = self.highlights_dock.isVisible()
        self.highlights_dock.setVisible(not is_visible)
        if is_visible:
            self.web_view.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.highlights_widget.focus()

    def toggle_lookup(self, force_show=False):
        self.lookup_dock.setVisible(force_show or not self.lookup_dock.isVisible())
        if force_show and self.lookup_dock.isVisible():
            self.lookup_widget.on_forced_show()

    def check_for_read_aloud(self, where: str):
        if self.actions_toolbar.toggle_read_aloud_action.isChecked():
            error_dialog(self, _('Cannot jump to location'), _(
                'The Read aloud feature is active, cannot jump to {}. Close it first.').format(where), show=True)
            return True
        return False

    def toc_clicked(self, index):
        if self.check_for_read_aloud(_('Table of Contents locations')):
            return
        item = self.toc_model.itemFromIndex(index)
        self.web_view.goto_toc_node(item.node_id)
        self.force_focus_on_web_view()

    def force_focus_on_web_view(self):
        self.activateWindow()
        self.web_view.setFocus(Qt.FocusReason.OtherFocusReason)

    def toc_searched(self, index):
        item = self.toc_model.itemFromIndex(index)
        self.web_view.goto_toc_node(item.node_id)

    def bookmarks_edited(self, bookmarks):
        self.current_book_data['annotations_map']['bookmark'] = bookmarks
        # annotations will be saved in book file on exit
        self.save_annotations(in_book_file=False)

    def goto_cfi(self, cfi, add_to_history=False):
        self.web_view.goto_cfi(cfi, add_to_history=add_to_history)

    def bookmark_activated(self, cfi):
        if self.check_for_read_aloud(_('bookmark')):
            return
        self.goto_cfi(cfi, add_to_history=True)

    def view_image(self, name):
        path = get_path_for_name(name)
        if path:
            pmap = QPixmap()
            if pmap.load(path):
                self.image_popup.current_img = pmap
                self.image_popup.current_url = QUrl.fromLocalFile(path)
                self.image_popup()
            else:
                error_dialog(self, _('Invalid image'), _(
                    'Failed to load the image {}').format(name), show=True)
        else:
            error_dialog(self, _('Image not found'), _(
                    'Failed to find the image {}').format(name), show=True)

    def copy_image(self, name):
        path = get_path_for_name(name)
        if not path:
            return error_dialog(self, _('Image not found'), _(
                'Failed to find the image {}').format(name), show=True)
        try:
            img = image_from_path(path)
        except Exception:
            return error_dialog(self, _('Invalid image'), _(
                'Failed to load the image {}').format(name), show=True)
        url = QUrl.fromLocalFile(path)
        md = QMimeData()
        md.setImageData(img)
        md.setUrls([url])
        QApplication.instance().clipboard().setMimeData(md)

    def dock_visibility_changed(self):
        vmap = {dock.objectName().partition('-')[0]: dock.toggleViewAction().isChecked() for dock in self.dock_widgets}
        self.actions_toolbar.update_dock_actions(vmap)
    # }}}

    # Load book {{{

    def show_loading_message(self, msg):
        if msg:
            self.loading_overlay(msg)
            self.actions_toolbar.update_action_state(False)
        else:
            if not hasattr(self, 'initial_loading_performace_reported'):
                performance_monitor('loading finished')
                self.initial_loading_performace_reported = True
            self.loading_overlay.hide()
            self.actions_toolbar.update_action_state(True)

    def content_file_changed(self, fname):
        if self.pending_search:
            search, self.pending_search = self.pending_search, None
            self.show_search(text=search['query'], trigger=True, search_type=search['type'], case_sensitive=search['case_sensitive'])

    def show_error(self, title, msg, details):
        self.loading_overlay.hide()
        error_dialog(self, title, msg, det_msg=details or None, show=True)

    def print_book(self):
        if not hasattr(set_book_path, 'pathtoebook'):
            error_dialog(self, _('Cannot print book'), _(
                'No book is currently open'), show=True)
            return
        from .printing import print_book
        print_book(set_book_path.pathtoebook, book_title=self.current_book_data['metadata']['title'], parent=self)

    @property
    def dock_widgets(self):
        return self.findChildren(QDockWidget, options=Qt.FindChildOption.FindDirectChildrenOnly)

    def reset_interface(self):
        for dock in self.dock_widgets:
            dock.setFloating(False)
            area = self.dock_defs[dock.objectName().partition('-')[0]].initial_area
            self.removeDockWidget(dock)
            self.addDockWidget(area, dock)
            dock.setVisible(False)

        for toolbar in self.findChildren(QToolBar):
            toolbar.setVisible(False)
            self.removeToolBar(toolbar)
            self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, toolbar)

    def ask_for_open_from_js(self, path):
        if path and not os.path.exists(path):
            self.web_view.remove_recently_opened(path)
            error_dialog(self, _('Book does not exist'), _(
                'Cannot open {} as it no longer exists').format(path), show=True)
        else:
            self.ask_for_open(path)

    def ask_for_open(self, path=None):
        if path is None:
            files = choose_files(
                self, 'ebook viewer open dialog',
                _('Choose e-book'), [(_('E-books'), available_input_formats())],
                all_files=False, select_only_single_file=True)
            if not files:
                return
            path = files[0]
        self.load_ebook(path)

    def continue_reading(self):
        rl = vprefs['session_data'].get('standalone_recently_opened')
        if rl:
            entry = rl[0]
            self.load_ebook(entry['pathtoebook'])

    def load_ebook(self, pathtoebook, open_at=None, reload_book=False):
        if '.' not in os.path.basename(pathtoebook):
            pathtoebook = os.path.abspath(os.path.realpath(pathtoebook))
        performance_monitor('Load of book started', reset=True)
        self.actions_toolbar.update_action_state(False)
        self.web_view.show_home_page_on_ready = False
        if open_at:
            self.pending_open_at = open_at
        self.setWindowTitle(_('Loading book') + f'… — {self.base_window_title}')
        self.loading_overlay(_('Loading book, please wait'))
        self.save_annotations()
        self.save_reading_rates()
        self.current_book_data = {}
        get_current_book_data(self.current_book_data)
        self.search_widget.clear_searches()
        t = Thread(name='LoadBook', target=self._load_ebook_worker, args=(pathtoebook, open_at, reload_book or self.force_reload))
        t.daemon = True
        t.start()

    def reload_book(self):
        if self.current_book_data:
            self.load_ebook(self.current_book_data['pathtoebook'], reload_book=True)

    def _load_ebook_worker(self, pathtoebook, open_at, reload_book):
        try:
            ans = prepare_book(pathtoebook, force=reload_book, prepare_notify=self.prepare_notify)
        except WorkerError as e:
            if not sip.isdeleted(self):
                self.book_prepared.emit(False, {'exception': e, 'tb': e.orig_tb, 'pathtoebook': pathtoebook})
        except Exception as e:
            import traceback
            if not sip.isdeleted(self):
                self.book_prepared.emit(False, {'exception': e, 'tb': traceback.format_exc(), 'pathtoebook': pathtoebook})
        else:
            performance_monitor('prepared emitted')
            if not sip.isdeleted(self):
                self.book_prepared.emit(True, {'base': ans, 'pathtoebook': pathtoebook, 'open_at': open_at, 'reloaded': reload_book})

    def prepare_notify(self):
        performance_monitor('preparation started')
        self.book_preparation_started.emit()

    def load_finished(self, ok, data):
        cbd = self.calibre_book_data_for_first_book
        self.calibre_book_data_for_first_book = None
        if self.shutting_down:
            return
        open_at, self.pending_open_at = self.pending_open_at, None
        self.pending_search = None
        self.web_view.clear_caches()
        if not ok:
            self.actions_toolbar.update_action_state(False)
            self.setWindowTitle(self.base_window_title)
            tb = as_unicode(data['tb'].strip(), errors='replace')
            tb = re.split(r'^calibre\.gui2\.viewer\.convert_book\.ConversionFailure:\s*', tb, maxsplit=1, flags=re.M)[-1]
            last_line = tuple(tb.strip().splitlines())[-1]
            if last_line.startswith('calibre.ebooks.DRMError'):
                DRMErrorMessage(self).exec()
            else:
                error_dialog(self, _('Loading book failed'), _(
                    'Failed to open the book at {0}. Click "Show details" for more info.').format(data['pathtoebook']),
                    det_msg=tb, show=True)
            self.loading_overlay.hide()
            self.web_view.show_home_page()
            return
        try:
            set_book_path(data['base'], data['pathtoebook'])
        except Exception:
            if data['reloaded']:
                raise
            self.load_ebook(data['pathtoebook'], open_at=data['open_at'], reload_book=True)
            return
        if iswindows:
            try:
                add_to_recent_docs(data['pathtoebook'])
            except Exception:
                import traceback
                traceback.print_exc()
        self.current_book_data = data
        get_current_book_data(self.current_book_data)
        self.current_book_data['annotations_map'] = defaultdict(list)
        self.current_book_data['annotations_path_key'] = path_key(data['pathtoebook']) + '.json'
        self.load_book_data(cbd)
        self.update_window_title()
        initial_cfi = self.initial_cfi_for_current_book()
        initial_position = {'type': 'cfi', 'data': initial_cfi} if initial_cfi else None
        if open_at:
            if open_at.startswith('toc:'):
                initial_toc_node = self.toc_model.node_id_for_text(open_at[len('toc:'):])
                initial_position = {'type': 'toc', 'data': initial_toc_node}
            elif open_at.startswith('toc-href:'):
                initial_toc_node = self.toc_model.node_id_for_href(open_at[len('toc-href:'):], exact=True)
                initial_position = {'type': 'toc', 'data': initial_toc_node}
            elif open_at.startswith('toc-href-contains:'):
                initial_toc_node = self.toc_model.node_id_for_href(open_at[len('toc-href-contains:'):], exact=False)
                initial_position = {'type': 'toc', 'data': initial_toc_node}
            elif open_at.startswith('epubcfi(/'):
                initial_position = {'type': 'cfi', 'data': open_at}
            elif open_at.startswith('ref:'):
                initial_position = {'type': 'ref', 'data': open_at[len('ref:'):]}
            elif open_at.startswith('search:'):
                self.pending_search = {'type': 'normal', 'query': open_at[len('search:'):], 'case_sensitive': False}
                initial_position = {'type': 'bookpos', 'data': 0}
            elif open_at.startswith('regex:'):
                self.pending_search = {'type': 'regex', 'query': open_at[len('regex:'):], 'case_sensitive': True}
                initial_position = {'type': 'bookpos', 'data': 0}
            elif is_float(open_at):
                initial_position = {'type': 'bookpos', 'data': float(open_at)}
        highlights = self.current_book_data['annotations_map']['highlight']
        self.highlights_widget.load(highlights)
        rates = load_reading_rates(self.current_book_data['annotations_path_key'])
        self.web_view.start_book_load(initial_position=initial_position, highlights=highlights, current_book_data=self.current_book_data, reading_rates=rates)
        performance_monitor('webview loading requested')

    def load_book_data(self, calibre_book_data=None):
        self.current_book_data['book_library_details'] = get_book_library_details(self.current_book_data['pathtoebook'])
        if calibre_book_data is not None:
            self.current_book_data['calibre_book_id'] = calibre_book_data['book_id']
            self.current_book_data['calibre_book_uuid'] = calibre_book_data['uuid']
            self.current_book_data['calibre_book_fmt'] = calibre_book_data['fmt']
            self.current_book_data['calibre_library_id'] = calibre_book_data['library_id']
        self.load_book_annotations(calibre_book_data)
        path = os.path.join(self.current_book_data['base'], 'calibre-book-manifest.json')
        with open(path, 'rb') as f:
            raw = f.read()
        self.current_book_data['manifest'] = manifest = json.loads(raw)
        toc = manifest.get('toc')
        self.toc_model = TOC(toc)
        self.toc.setModel(self.toc_model)
        self.bookmarks_widget.set_bookmarks(self.current_book_data['annotations_map']['bookmark'])
        self.current_book_data['metadata'] = set_book_path.parsed_metadata
        self.current_book_data['manifest'] = set_book_path.parsed_manifest

    def load_book_annotations(self, calibre_book_data=None):
        amap = self.current_book_data['annotations_map']
        path = os.path.join(self.current_book_data['base'], 'calibre-book-annotations.json')
        if os.path.exists(path):
            with open(path, 'rb') as f:
                raw = f.read()
            merge_annotations(parse_annotations(raw), amap)
        path = os.path.join(annotations_dir, self.current_book_data['annotations_path_key'])
        if os.path.exists(path):
            with open(path, 'rb') as f:
                raw = f.read()
            merge_annotations(parse_annotations(raw), amap)
        if calibre_book_data is None:
            bld = self.current_book_data['book_library_details']
            if bld is not None:
                lib_amap = load_annotations_map_from_library(bld)
                sau = get_session_pref('sync_annots_user', default='')
                if sau:
                    other_amap = load_annotations_map_from_library(bld, user_type='web', user=sau)
                    if other_amap:
                        merge_annotations(other_amap, lib_amap)
                if lib_amap:
                    for annot_type, annots in iteritems(lib_amap):
                        merge_annotations(annots, amap)
        else:
            for annot_type, annots in iteritems(calibre_book_data['annotations_map']):
                merge_annotations(annots, amap)

    def update_window_title(self):
        try:
            title = self.current_book_data['metadata']['title']
        except Exception:
            title = _('Unknown')
        book_format = self.current_book_data['manifest']['book_format']
        title = f'{title} [{book_format}] — {self.base_window_title}'
        self.setWindowTitle(title)
    # }}}

    # CFI management {{{
    def initial_cfi_for_current_book(self):
        lrp = self.current_book_data['annotations_map']['last-read']
        if lrp and get_session_pref('remember_last_read', default=True):
            lrp = lrp[0]
            if lrp['pos_type'] == 'epubcfi':
                return lrp['pos']

    def cfi_changed(self, cfi):
        if not self.current_book_data:
            return
        self.current_book_data['annotations_map']['last-read'] = [{
            'pos': cfi, 'pos_type': 'epubcfi', 'timestamp': utcnow().isoformat()}]
        self.save_pos_timer.start()
    # }}}

    # State serialization {{{
    def save_annotations(self, in_book_file=True):
        if not self.current_book_data:
            return
        if self.annotations_saver is None:
            self.annotations_saver = AnnotationsSaveWorker()
            self.annotations_saver.start()
        self.annotations_saver.save_annotations(
            self.current_book_data,
            in_book_file and get_session_pref('save_annotations_in_ebook', default=True),
            get_session_pref('sync_annots_user', default='')
        )

    def update_reading_rates(self, rates):
        if not self.current_book_data:
            return
        self.current_book_data['reading_rates'] = rates
        self.save_reading_rates()

    def save_reading_rates(self):
        if not self.current_book_data:
            return
        key = self.current_book_data.get('annotations_path_key')
        rates = self.current_book_data.get('reading_rates')
        if key and rates:
            save_reading_rates(key, rates)

    def highlights_changed(self, highlights):
        if not self.current_book_data:
            return
        amap = self.current_book_data['annotations_map']
        amap['highlight'] = highlights
        self.highlights_widget.refresh(highlights)
        self.save_annotations()

    def notes_edited(self, uuid, notes):
        for h in self.current_book_data['annotations_map']['highlight']:
            if h.get('uuid') == uuid:
                h['notes'] = notes
                h['timestamp'] = utcnow().isoformat()
                break
        else:
            return
        self.save_annotations()

    def edit_book(self, file_name, progress_frac, selected_text):
        import subprocess

        from calibre.ebooks.oeb.polish.main import SUPPORTED
        from calibre.utils.ipc.launch import exe_path, macos_edit_book_bundle_path
        try:
            path = set_book_path.pathtoebook
        except AttributeError:
            return error_dialog(self, _('Cannot edit book'), _(
                'No book is currently open'), show=True)
        fmt = path.rpartition('.')[-1].upper().replace('ORIGINAL_', '')
        if fmt not in SUPPORTED:
            return error_dialog(self, _('Cannot edit book'), _(
                'The book must be in the %s formats to edit.'
                '\n\nFirst convert the book to one of these formats.'
            ) % (_(' or ').join(SUPPORTED)), show=True)
        exe = 'ebook-edit'
        if ismacos:
            exe = os.path.join(macos_edit_book_bundle_path(), exe)
        else:
            exe = exe_path(exe)

        cmd = [exe] if isinstance(exe, str) else list(exe)
        if selected_text:
            cmd += ['--select-text', selected_text]
        from calibre.gui2.widgets import BusyCursor
        with sanitize_env_vars():
            subprocess.Popen(cmd + [path, file_name])
            with BusyCursor():
                time.sleep(2)

    def save_state(self):
        with vprefs:
            vprefs['main_window_state'] = bytearray(self.saveState(self.MAIN_WINDOW_STATE_VERSION))
            self.save_geometry(vprefs, 'main_window_geometry')

    def restore_state(self):
        state = vprefs['main_window_state']
        if not get_session_pref('remember_window_geometry', default=False) or not self.restore_geometry(vprefs, 'main_window_geometry'):
            QApplication.instance().ensure_window_on_screen(self)
        if state:
            self.restoreState(state, self.MAIN_WINDOW_STATE_VERSION)
            self.inspector_dock.setVisible(False)
            if not get_session_pref('restore_docks', True):
                for dock_def in self.dock_defs.values():
                    d = getattr(self, '{}_dock'.format(dock_def.name.partition('-')[0]))
                    d.setVisible(False)

    def quit(self):
        self.close()

    def force_close(self):
        if not self.close_forced:
            self.close_forced = True
            self.quit()

    def close_prep_finished(self, cfi):
        if cfi:
            self.cfi_changed(cfi)
        self.force_close()

    def request_close(self):
        self.close()

    def closeEvent(self, ev):
        if self.shutdown_done:
            return
        if self.current_book_data and self.web_view.view_is_ready and not self.close_forced:
            ev.ignore()
            if not self.shutting_down:
                self.shutting_down = True
                QTimer.singleShot(2000, self.force_close)
                self.web_view.prepare_for_close()
            return
        self.shutting_down = True
        self.search_widget.shutdown()
        self.web_view.shutdown()
        try:
            self.save_state()
            self.save_annotations()
            self.save_reading_rates()
            if self.annotations_saver is not None:
                self.annotations_saver.shutdown()
                self.annotations_saver = None
        except Exception:
            import traceback
            traceback.print_exc()
        self.shutdown_done = True
        return MainWindow.closeEvent(self, ev)
    # }}}

    # Auto-hide mouse cursor {{{
    def setup_mouse_auto_hide(self):
        QApplication.instance().installEventFilter(self)
        self.cursor_hidden = False
        self.hide_cursor_timer = t = QTimer(self)
        t.setSingleShot(True), t.setInterval(3000)
        t.timeout.connect(self.hide_cursor)
        t.start()

    def eventFilter(self, obj, ev):
        et = ev.type()
        if et == QEvent.Type.MouseMove:
            if self.cursor_hidden:
                self.cursor_hidden = False
                QApplication.instance().restoreOverrideCursor()
            self.hide_cursor_timer.start()
        elif et == QEvent.Type.FocusIn:
            if iswindows and obj and obj.objectName() == 'EbookViewerClassWindow' and self.isFullScreen():
                # See https://bugs.launchpad.net/calibre/+bug/1918591
                self.web_view.repair_after_fullscreen_switch()
        return False

    def hide_cursor(self):
        if get_session_pref('auto_hide_mouse', True):
            self.cursor_hidden = True
            QApplication.instance().setOverrideCursor(Qt.CursorShape.BlankCursor)
    # }}}
