from qt.core import QToolButton, QIcon, Qt, QWidget, QAction
from qt.core import (
    QAction, QApplication, QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QIcon,
    QKeySequence, QLabel, QPainter, QPlainTextEdit, QSize, QSizePolicy, Qt,
    QTextBrowser, QTextDocument, QVBoxLayout, QWidget, pyqtSignal,
)
from calibre.gui2.dialogs.message_box import Icon

class ScrollTopButton(QToolButton):

    def __init__(self, gui):

        sc = 'T'

        QToolButton.__init__(self)
        self.gui = gui
        self.setAutoRaise(True), self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setIcon(QIcon.ic('top.png'))

        self.action_toggle = QAction(self.icon(),'置顶', self)

        gui.addAction(self.action_toggle)
        gui.keyboard.register_shortcut('置顶', '置顶',
                                    default_keys=(sc,), action=self.action_toggle)
        self.action_toggle.triggered.connect(self.scroll_top)
        self.clicked.connect(self.scroll_top)

    def scroll_top(self):
        self.gui.library_view.selectRow(0)
        self.gui.library_view.selectionModel().reset()


class Spacer(QWidget):

    def __init__(self, width=0):
        QWidget.__init__(self)
        self.setFixedWidth(width)
        self.setHidden(1)
        self.setVisible(1)


class JumpToFolderBox(QDialog):  # {{{
    
    # The dialog used in plugins, used to open the temp folder of the container.

    ERROR = 0
    WARNING = 1
    INFO = 2
    QUESTION = 3

    resize_needed = pyqtSignal()

    def setup_ui(self):
        self.setObjectName("Dialog")
        self.resize(497, 235)
        self.gridLayout = l = QGridLayout(self)
        l.setObjectName("gridLayout")
        self.icon_widget = Icon(self)
        l.addWidget(self.icon_widget)
        self.msg = la = QLabel(self)
        la.setWordWrap(True), la.setMinimumWidth(400)
        la.setOpenExternalLinks(True)
        la.setObjectName("msg")
        l.addWidget(la, 0, 1, 1, 1)
        self.det_msg = dm = QTextBrowser(self)
        dm.setReadOnly(True)
        dm.setObjectName("det_msg")
        l.addWidget(dm, 1, 0, 1, 2)
        self.bb = bb = QDialogButtonBox(self)
        bb.setStandardButtons(QDialogButtonBox.StandardButton.Ok)
        bb.setObjectName("bb")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        l.addWidget(bb, 3, 0, 1, 2)
        self.toggle_checkbox = tc = QCheckBox(self)
        tc.setObjectName("toggle_checkbox")
        l.addWidget(tc, 2, 0, 1, 2)

    def __init__(self, type_, title, msg,
                 det_msg='',
                 q_icon=None,
                 show_copy_button=True,
                 parent=None, default_yes=True,
                 yes_text=None, no_text=None, yes_icon=None, no_icon=None,
                 add_abort_button=False,
                 only_copy_details=False
    ):
        QDialog.__init__(self, parent)
        self.only_copy_details = only_copy_details
        self.aborted = False
        if q_icon is None:
            icon = {
                    self.ERROR : 'error',
                    self.WARNING: 'warning',
                    self.INFO:    'information',
                    self.QUESTION: 'question',
            }[type_]
            icon = 'dialog_%s.png'%icon
            self.icon = QIcon.ic(icon)
        else:
            self.icon = q_icon if isinstance(q_icon, QIcon) else QIcon.ic(q_icon)
        self.setup_ui()

        self.setWindowTitle(title)
        self.setWindowIcon(self.icon)
        self.icon_widget.set_icon(self.icon)
        self.msg.setText(msg)
        if det_msg and Qt.mightBeRichText(det_msg):
            self.det_msg.setHtml(det_msg)
        else:
            self.det_msg.setPlainText(det_msg)
        self.det_msg.setVisible(False)
        self.toggle_checkbox.setVisible(False)

        if show_copy_button:
            self.ctc_button = self.bb.addButton('打开文件夹',
                    QDialogButtonBox.ButtonRole.ActionRole)
            self.ctc_button.clicked.connect(self.copy_to_clipboard)

        self.show_det_msg = _('Show &details')
        self.hide_det_msg = _('Hide &details')
        self.det_msg_toggle = self.bb.addButton(self.show_det_msg, QDialogButtonBox.ButtonRole.ActionRole)
        self.det_msg_toggle.clicked.connect(self.toggle_det_msg)
        self.det_msg_toggle.setToolTip(
                _('Show detailed information about this error'))

        self.copy_action = QAction(self)
        self.addAction(self.copy_action)
        self.copy_action.setShortcuts(QKeySequence.StandardKey.Copy)
        self.copy_action.triggered.connect(self.copy_to_clipboard)

        self.is_question = type_ == self.QUESTION
        if self.is_question:
            self.bb.setStandardButtons(QDialogButtonBox.StandardButton.Yes|QDialogButtonBox.StandardButton.No)
            self.bb.button(QDialogButtonBox.StandardButton.Yes if default_yes else QDialogButtonBox.StandardButton.No
                    ).setDefault(True)
            self.default_yes = default_yes
            if yes_text is not None:
                self.bb.button(QDialogButtonBox.StandardButton.Yes).setText(yes_text)
            if no_text is not None:
                self.bb.button(QDialogButtonBox.StandardButton.No).setText(no_text)
            if yes_icon is not None:
                self.bb.button(QDialogButtonBox.StandardButton.Yes).setIcon(yes_icon if isinstance(yes_icon, QIcon) else QIcon.ic(yes_icon))
            if no_icon is not None:
                self.bb.button(QDialogButtonBox.StandardButton.No).setIcon(no_icon if isinstance(no_icon, QIcon) else QIcon.ic(no_icon))
        else:
            self.bb.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)

        if add_abort_button:
            self.bb.addButton(QDialogButtonBox.StandardButton.Abort).clicked.connect(self.on_abort)

        if not det_msg:
            self.det_msg_toggle.setVisible(False)

        self.resize_needed.connect(self.do_resize, type=Qt.ConnectionType.QueuedConnection)
        self.do_resize()

    def on_abort(self):
        self.aborted = True

    def sizeHint(self):
        ans = QDialog.sizeHint(self)
        ans.setWidth(max(min(ans.width(), 500), self.bb.sizeHint().width() + 100))
        ans.setHeight(min(ans.height(), 500))
        return ans

    def toggle_det_msg(self, *args):
        vis = self.det_msg.isVisible()
        self.det_msg.setVisible(not vis)
        self.det_msg_toggle.setText(self.show_det_msg if vis else self.hide_det_msg)
        self.resize_needed.emit()

    def do_resize(self):
        self.resize(self.sizeHint())

    def copy_to_clipboard(self, *args):
        file_to_show = self.det_msg.toPlainText()
        import subprocess
        subprocess.call(["open", file_to_show])

    def showEvent(self, ev):
        ret = QDialog.showEvent(self, ev)
        if self.is_question:
            try:
                self.bb.button(QDialogButtonBox.StandardButton.Yes if self.default_yes else QDialogButtonBox.StandardButton.No
                        ).setFocus(Qt.FocusReason.OtherFocusReason)
            except:
                pass  # Buttons were changed
        else:
            self.bb.button(QDialogButtonBox.StandardButton.Ok).setFocus(Qt.FocusReason.OtherFocusReason)
        return ret

    def set_details(self, msg):
        if not msg:
            msg = ''
        if Qt.mightBeRichText(msg):
            self.det_msg.setHtml(msg)
        else:
            self.det_msg.setPlainText(msg)
        self.det_msg_toggle.setText(self.show_det_msg)
        self.det_msg_toggle.setVisible(bool(msg))
        self.det_msg.setVisible(False)
        self.resize_needed.emit()
# }}}