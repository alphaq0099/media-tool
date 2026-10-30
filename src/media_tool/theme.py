from pathlib import Path
from PySide6.QtGui import QColor, QPalette
from PySide6.QtCore import QObject, QEvent
from PySide6.QtWidgets import QWidget


class ThemeRefresh(QObject):
    """Refresh visible style brushes now and hidden controls before first paint."""
    def refresh(self, host):
        for widget in host.findChildren(QWidget):
            if widget.isVisible():
                widget.style().polish(widget)
            else:
                widget.installEventFilter(self)

    def eventFilter(self, widget, event):
        if event.type() == QEvent.Type.Show:
            widget.removeEventFilter(self)
            widget.style().polish(widget)
        return False

THEMES = {
    "light": dict(bg="#dce3dd", panel="#e8ede6", text="#25332c", muted="#536359",
                  line="#bac8bd", accent="#27745d", soft="#c8ded0", button="#f3f6ef", warning="#8a4100"),
    "dark": dict(bg="#15191f", panel="#1d232d", text="#e5ebf3", muted="#a9b4c4",
                 line="#343d49", accent="#5bd4bf", soft="#183c3e", button="#102522", warning="#ffd18a"),
}

def apply_theme(app, name):
    c = THEMES.get(name, THEMES["light"])
    palette = QPalette()
    for role, key in ((QPalette.ColorRole.Window, "bg"),
                      (QPalette.ColorRole.WindowText, "text"),
                      (QPalette.ColorRole.Base, "panel"),
                      (QPalette.ColorRole.AlternateBase, "bg"),
                      (QPalette.ColorRole.Text, "text"),
                      (QPalette.ColorRole.Button, "panel"),
                      (QPalette.ColorRole.ButtonText, "text"),
                      (QPalette.ColorRole.Highlight, "soft"),
                      (QPalette.ColorRole.HighlightedText, "text"),
                      (QPalette.ColorRole.ToolTipBase, "panel"),
                      (QPalette.ColorRole.ToolTipText, "text"),
                      (QPalette.ColorRole.PlaceholderText, "muted"),
                      (QPalette.ColorRole.Mid, "line"),
                      (QPalette.ColorRole.Link, "accent"),
                      (QPalette.ColorRole.LinkVisited, "button"),
                      (QPalette.ColorRole.BrightText, "warning")):
        palette.setColor(role, QColor(c[key]))
    app.setPalette(palette)
    sheet = """
QWidget {color: palette(text); font-family: "Segoe UI"; font-size: 13px; font-weight: 400;}
QMainWindow, QWidget#workspace {background: palette(window);}
QFrame#sidebar, QFrame#panel {background: palette(base);}
QWidget#folderOptions {background: palette(base);}
QFrame#sidebar {border-right: 1px solid palette(mid);}
QFrame#panel {border: 1px solid palette(mid); border-radius: 9px;}
QLabel {background: transparent;}
QLabel#brand {font-size: 19px; font-weight: 700;}
QLabel#heading {font-size: 27px; font-weight: 700;}
QLabel#section {font-size: 14px; font-weight: 600;}
QLabel#muted {color: palette(placeholder-text);}
QLabel#warning {color: palette(bright-text); font-weight: 600;}
QLabel#badge {color: palette(link); background: palette(highlight); border-radius: 5px; padding: 6px 10px;}
QLabel#drop {border: 1px dashed palette(placeholder-text); border-radius: 6px; padding: 14px; color: palette(placeholder-text);}
QPushButton, QToolButton#openButton {background: palette(base); border: 1px solid palette(mid); border-radius: 6px; padding: 9px 13px;}
QPushButton:hover, QToolButton#openButton:hover {background: palette(highlight);}
QPushButton:focus, QToolButton#openButton:focus, QComboBox:focus, QLineEdit:focus {border: 2px solid palette(link);}
QPushButton#nav {text-align: left; background: transparent; border: none; border-left: 3px solid transparent; padding: 12px 9px; border-radius: 6px;}
QPushButton#nav:hover {background: palette(base);}
QPushButton#nav:checked {background: palette(highlight); color: palette(link); border-left-color: palette(link); font-weight: 600;}
QPushButton#primary {background: palette(link); color: palette(link-visited); font-weight: 600;}
QPushButton#primary:disabled {background: palette(window); color: palette(placeholder-text); border: 1px solid palette(mid);}
QPushButton:disabled, QToolButton#openButton:disabled {color: palette(placeholder-text); background: palette(window);}
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {background: palette(window); border: 1px solid palette(mid); padding: 8px; border-radius: 5px; min-height: 16px;}
QComboBox {padding-right: 23px;}
QSpinBox, QDoubleSpinBox {padding-right: 26px;}
QComboBox {combobox-popup: 0;}
QComboBox::drop-down {subcontrol-origin: border; subcontrol-position: top right; width: 20px; border: none;}
QComboBox::down-arrow {image: url("@icons@/down.svg"); width: 10px; height: 10px;}
QSpinBox::up-button, QDoubleSpinBox::up-button {subcontrol-origin: border; subcontrol-position: top right; width: 25px; background: palette(highlight); border-left: 1px solid palette(placeholder-text); border-bottom: 1px solid palette(mid); border-top-right-radius: 5px;}
QSpinBox::down-button, QDoubleSpinBox::down-button {subcontrol-origin: border; subcontrol-position: bottom right; width: 25px; background: palette(highlight); border-left: 1px solid palette(placeholder-text); border-bottom-right-radius: 5px;}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {image: url("@icons@/up.svg"); width: 12px; height: 12px;}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {image: url("@icons@/down.svg"); width: 12px; height: 12px;}
QWidget[mediaTheme="dark"] QSpinBox::up-arrow, QWidget[mediaTheme="dark"] QDoubleSpinBox::up-arrow {image: url("@icons@/up-dark.svg");}
QWidget[mediaTheme="dark"] QComboBox::down-arrow, QWidget[mediaTheme="dark"] QSpinBox::down-arrow, QWidget[mediaTheme="dark"] QDoubleSpinBox::down-arrow {image: url("@icons@/down-dark.svg");}
QSlider:horizontal {min-height: 28px;}
QSlider::groove:horizontal {height: 6px; background: palette(mid); border-radius: 3px;}
QSlider::sub-page:horizontal {background: palette(link); border-radius: 3px;}
QSlider::handle:horizontal {background: palette(link); border: 2px solid palette(base); width: 16px; margin: -7px 0; border-radius: 10px;}
QSlider::handle:horizontal:hover, QSlider::handle:horizontal:focus {border-color: palette(text);}
QSlider::handle:horizontal:disabled {background: palette(placeholder-text);}
QCheckBox {spacing: 7px;}
QCheckBox::indicator {width: 17px; height: 17px; border: 2px solid palette(placeholder-text); border-radius: 3px; background: palette(window);}
QCheckBox::indicator:checked {image: url("@icons@/check.svg"); border-color: palette(link);}
QCheckBox::indicator:hover {border-color: palette(link);}
QToolButton#openButton {padding: 8px 32px 8px 14px;}
QToolButton#openButton::menu-button {subcontrol-origin: border; subcontrol-position: top right; width: 24px; border-left: 1px solid palette(placeholder-text); background: palette(highlight); border-top-right-radius: 6px; border-bottom-right-radius: 6px;}
QToolButton#openButton::menu-arrow, QPushButton::menu-indicator {image: url("@icons@/down.svg"); width: 12px; height: 12px;}
QPushButton::menu-indicator {subcontrol-origin: padding; subcontrol-position: center right; right: 4px;}
QPushButton[hasMenu="true"] {padding-right: 26px;}
QComboBox QAbstractItemView {background: palette(base); color: palette(text); border: 1px solid palette(link); selection-background-color: palette(link); selection-color: palette(link-visited); outline: none;}
QComboBox QAbstractItemView::item {min-height: 30px; padding: 4px 8px;}
QComboBox QAbstractItemView::item:selected {background: palette(link); color: palette(link-visited);}
QMenu {background: palette(base); border: 1px solid palette(mid);}
QMenu::item {padding: 8px 20px;}
QMenu::item:selected {background: palette(link); color: palette(link-visited);}
QTabWidget::pane {border: none; background: palette(base);}
QTabBar::tab {background: transparent; color: palette(placeholder-text); padding: 12px 18px; border-bottom: 2px solid transparent;}
QTabBar::tab:selected {background: palette(base); color: palette(link); border-bottom: 2px solid palette(link);}
QScrollArea {background: palette(window); border: none;}
QTableView, QTreeView {background: palette(base); border: none; gridline-color: palette(mid); selection-background-color: palette(highlight);}
QTableView::item {padding: 6px 10px;}
QTreeView::item {min-height: 30px; padding: 3px 0;}
QHeaderView {background: palette(window); border: none;}
QHeaderView::section {background: palette(window); color: palette(placeholder-text); border: none; padding: 10px; text-align: left;}
QHeaderView::up-arrow {image: url("@icons@/up.svg"); width: 12px; height: 12px;}
QHeaderView::down-arrow {image: url("@icons@/down.svg"); width: 12px; height: 12px;}
QProgressBar {background: palette(mid); border: none; border-radius: 4px; min-height: 8px; max-height: 8px;}
QProgressBar::chunk {background: palette(link); border-radius: 3px;}
QScrollBar:vertical {background: palette(window); width: 13px; margin: 0; border-left: 1px solid palette(mid);}
QScrollBar::handle:vertical {background: palette(placeholder-text); min-height: 26px; border-radius: 5px; margin: 2px;}
QScrollBar::handle:vertical:hover {background: palette(link);}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {height: 0;}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {background: none;}
QScrollBar:horizontal {background: palette(window); height: 13px; margin: 0; border-top: 1px solid palette(mid);}
QScrollBar::handle:horizontal {background: palette(placeholder-text); min-width: 26px; border-radius: 5px; margin: 2px;}
QScrollBar::handle:horizontal:hover {background: palette(link);}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {width: 0;}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {background: none;}
QToolTip {background: palette(base); color: palette(text); border: 1px solid palette(mid);}
QFrame#inspectorGroup, QFrame#activityPanel {background: palette(base); border: 1px solid palette(mid); border-radius: 7px;}
QPushButton#groupHeader {text-align: left; font-weight: 600; border: none; background: transparent; padding: 8px 0;}
QToolButton#info {border: 1px solid palette(mid); border-radius: 9px; color: palette(placeholder-text); background: transparent; padding: 0; font-size: 12px;}
QLabel#activityTitle {font-size: 17px; font-weight: 600;}
QPlainTextEdit {background: palette(window); border: 1px solid palette(mid); border-radius: 5px; padding: 6px;}
QFrame#flowStep {background: palette(window); border: 1px solid palette(mid); border-radius: 6px;}
QFrame#flowStep[active="true"] {border-color: palette(link);}
QLabel#flowArrow {color: palette(link); font-size: 20px;}
QListWidget#workflowSteps::item {padding: 7px 8px;}
"""
    sheet = sheet.replace("@icons@", Path(__file__).with_name("icons").as_posix())
    # Color changes use the palette; do not reparse/repolish the whole widget tree.
    if app.styleSheet() != sheet:
        app.setStyleSheet(sheet)
