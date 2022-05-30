from qt.core import QToolButton, QIcon, Qt, QWidget, QAction

class ScrollTopButton(QToolButton):

    def __init__(self, gui):

        sc = 'T'

        QToolButton.__init__(self)
        self.gui = gui
        self.setAutoRaise(True), self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setIcon(QIcon(I('top.png')))

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