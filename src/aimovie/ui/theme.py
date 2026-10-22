"""界面主题（深色）。"""

from __future__ import annotations

DARK_STYLESHEET = """
* {
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog {
    background-color: #1e1f22;
    color: #e8e8e8;
}
QWidget {
    color: #e8e8e8;
}
QWidget#LeftPanel {
    background-color: #191a1d;
    border-right: 1px solid #303238;
}
QLabel#PanelTitle {
    font-size: 17px;
    font-weight: 600;
    padding: 4px 2px;
}
QLabel#ProjectName {
    font-size: 20px;
    font-weight: 600;
    color: #ffffff;
}
QLabel#StatusBadge {
    font-weight: 600;
    color: #7fb3ff;
}
QLabel#HintLabel {
    color: #9aa0aa;
}
QLabel#PreviewLabel {
    background-color: #121316;
    border: 1px solid #3a3d45;
    border-radius: 6px;
    color: #8d939e;
}
QGroupBox {
    border: 1px solid #34373e;
    border-radius: 8px;
    margin-top: 12px;
    padding: 12px 10px 10px 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: #a9b0bb;
}
QPushButton {
    background-color: #2b2d33;
    border: 1px solid #3c3f47;
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 20px;
}
QPushButton:hover {
    background-color: #343741;
    border-color: #4a4e58;
}
QPushButton:pressed {
    background-color: #262830;
}
QPushButton:disabled {
    color: #6b707a;
    background-color: #24262b;
    border-color: #303239;
}
QPushButton#PrimaryButton {
    background-color: #2f6fed;
    border: 1px solid #427cf0;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#PrimaryButton:hover {
    background-color: #3b7bf5;
}
QPushButton#PrimaryButton:disabled {
    background-color: #2a3a57;
    border-color: #33435e;
    color: #8294b3;
}
QPushButton#DangerButton {
    color: #ff9b9b;
    border-color: #5b3438;
}
QPushButton#DangerButton:hover {
    background-color: #3a2528;
}
QListWidget {
    background-color: #202127;
    border: 1px solid #303239;
    border-radius: 8px;
    outline: none;
    padding: 4px;
}
QListWidget::item {
    padding: 8px 10px;
    border-radius: 6px;
    margin: 2px 0;
    color: #d7d9de;
}
QListWidget::item:selected {
    background-color: #2d4f86;
    color: #ffffff;
}
QListWidget::item:hover:!selected {
    background-color: #2a2c33;
}
QTabWidget::pane {
    border: 1px solid #303239;
    border-radius: 8px;
    top: -1px;
    background-color: #202127;
}
QTabBar::tab {
    background: transparent;
    color: #9aa0aa;
    padding: 8px 18px;
    margin-right: 4px;
    border: 1px solid transparent;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
}
QTabBar::tab:selected {
    color: #ffffff;
    background-color: #202127;
    border-color: #303239;
    border-bottom-color: #202127;
}
QTabBar::tab:hover:!selected {
    color: #d0d4db;
}
QTableWidget {
    background-color: #202127;
    alternate-background-color: #23242b;
    border: 1px solid #303239;
    border-radius: 6px;
    gridline-color: #2c2f36;
    selection-background-color: #2d4f86;
    selection-color: #ffffff;
}
QHeaderView::section {
    background-color: #292b32;
    color: #aeb4bf;
    border: none;
    border-right: 1px solid #33363e;
    border-bottom: 1px solid #33363e;
    padding: 7px 8px;
    font-weight: 600;
}
QTableCornerButton::section {
    background-color: #292b32;
    border: none;
}
QProgressBar {
    border: 1px solid #383b43;
    border-radius: 6px;
    background-color: #26282e;
    text-align: center;
    color: #e8e8e8;
    min-height: 18px;
}
QProgressBar::chunk {
    background-color: #2f6fed;
    border-radius: 5px;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background-color: #26282e;
    border: 1px solid #3a3d45;
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: #2d4f86;
    min-height: 20px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: #2f6fed;
}
QComboBox QAbstractItemView {
    background-color: #26282e;
    border: 1px solid #3a3d45;
    selection-background-color: #2d4f86;
}
QPlainTextEdit, QTextBrowser {
    background-color: #1a1b1f;
    border: 1px solid #303239;
    border-radius: 6px;
    selection-background-color: #2d4f86;
    color: #cfd3da;
}
QMenuBar {
    background-color: #1e1f22;
    border-bottom: 1px solid #303239;
}
QMenuBar::item {
    background: transparent;
    padding: 6px 12px;
}
QMenuBar::item:selected {
    background-color: #2d2f36;
    border-radius: 4px;
}
QMenu {
    background-color: #26282e;
    border: 1px solid #3a3d45;
    padding: 4px;
}
QMenu::item {
    padding: 7px 26px 7px 18px;
    border-radius: 4px;
}
QMenu::item:selected {
    background-color: #2d4f86;
}
QStatusBar {
    background-color: #191a1d;
    border-top: 1px solid #303239;
    color: #9aa0aa;
}
QToolTip {
    background-color: #2b2d33;
    color: #e8e8e8;
    border: 1px solid #454952;
    padding: 4px 6px;
}
QSplitter::handle {
    background-color: #2a2c32;
    width: 3px;
    height: 3px;
}
QScrollBar:vertical {
    background: #202127;
    width: 12px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #3d414a;
    min-height: 30px;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover {
    background: #4a4f5a;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: #202127;
    height: 12px;
}
QScrollBar::handle:horizontal {
    background: #3d414a;
    min-width: 30px;
    border-radius: 5px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
QCheckBox {
    spacing: 8px;
}
QDialogButtonBox QPushButton {
    min-width: 84px;
}
"""
