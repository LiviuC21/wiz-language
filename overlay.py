"""overlay.py - frameless, translucent, always-on-top PyQt5 overlay (UI only, no heavy work).

Public interface (unchanged, so main.py keeps working):
    signals: text_submitted(str), source_language_changed(object), device_chosen(object)
    slots:   set_status, set_devices, add_incoming, add_outgoing
"""
from __future__ import annotations

import html
from typing import List, Optional

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal

import config

STYLE = """
#panel, #dlgPanel { background: rgba(14, 16, 22, 190); border: 1px solid rgba(255,255,255,45);
                    border-radius: 12px; }
QLabel { color: #9aa4b2; font-size: 11px; }
QLabel#title { color: #e6ebf2; font-size: 12px; font-weight: bold; }

/* Chat history: transparent, no font rule here so setFont() controls the size */
QTextBrowser { background: transparent; border: none; color: #ffffff;
               selection-background-color: rgba(127,209,255,90); }
QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }
QScrollBar::handle:vertical { background: rgba(255,255,255,90); border-radius: 3px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: rgba(255,255,255,150); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

QLineEdit { background: rgba(255,255,255,28); border: 1px solid rgba(255,255,255,55);
            border-radius: 8px; color: #ffffff; padding: 6px 8px; font-size: 14px; }
QComboBox { background: rgba(255,255,255,28); color: #fff; border: 1px solid rgba(255,255,255,45);
            border-radius: 6px; padding: 3px 8px; font-size: 12px; }
QComboBox QAbstractItemView { background: #1b1e27; color: #fff; selection-background-color: #3b4a6b; }

QPushButton { background: transparent; color: #cbd2dc; border: none; font-size: 13px; }
QPushButton#close:hover { color: #ff6b6b; }
QPushButton#gear { font-size: 16px; }
QPushButton#gear:hover { color: #7fd1ff; }

QSlider::groove:horizontal { height: 4px; background: rgba(255,255,255,60); border-radius: 2px; }
QSlider::sub-page:horizontal { background: #7fd1ff; border-radius: 2px; }
QSlider::handle:horizontal { background: #e6ebf2; width: 14px; margin: -6px 0; border-radius: 7px; }
"""


class SettingsDialog(QtWidgets.QDialog):
    """Small non-modal settings popup.

    It deliberately does NOT take keyboard focus (WindowDoesNotAcceptFocus): your game
    stays the active window, which keeps the "return focus to the game" logic in
    output.py working. Everything is mouse-operable (combo, slider, close button).
    """

    source_language_changed = pyqtSignal(object)   # str | None
    font_size_changed = pyqtSignal(int)

    def __init__(self, parent: QtWidgets.QWidget, font_size: int) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint |
                            Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedWidth(290)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QtWidgets.QFrame()
        panel.setObjectName("dlgPanel")
        panel.setStyleSheet(STYLE)
        outer.addWidget(panel)

        lay = QtWidgets.QVBoxLayout(panel)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(10)

        # --- title row
        head = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Settings")
        title.setObjectName("title")
        close_btn = QtWidgets.QPushButton("\u2715")
        close_btn.setObjectName("close")
        close_btn.setFixedSize(22, 22)
        close_btn.clicked.connect(self.hide)
        head.addWidget(title, 1)
        head.addWidget(close_btn)
        lay.addLayout(head)

        # --- source language
        lay.addWidget(QtWidgets.QLabel("Source language (speech in the game)"))
        self._lang = QtWidgets.QComboBox()
        for label, code in config.SOURCE_LANGUAGES.items():
            self._lang.addItem(label, code)
        self._lang.currentIndexChanged.connect(
            lambda _i: self.source_language_changed.emit(self._lang.currentData()))
        lay.addWidget(self._lang)

        # --- font size
        lay.addWidget(QtWidgets.QLabel("Font size"))
        row = QtWidgets.QHBoxLayout()
        self._slider = QtWidgets.QSlider(Qt.Horizontal)
        self._slider.setRange(config.FONT_SIZE_MIN, config.FONT_SIZE_MAX)
        self._slider.setValue(font_size)
        self._size_label = QtWidgets.QLabel(f"{font_size} pt")
        self._size_label.setMinimumWidth(40)
        self._size_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._slider.valueChanged.connect(self._on_size_changed)   # fires continuously while dragging
        row.addWidget(self._slider, 1)
        row.addWidget(self._size_label)
        lay.addLayout(row)

    def _on_size_changed(self, value: int) -> None:
        self._size_label.setText(f"{value} pt")
        self.font_size_changed.emit(value)


class OverlayWindow(QtWidgets.QWidget):
    text_submitted = pyqtSignal(str)              # Romanian text typed by the user
    source_language_changed = pyqtSignal(object)  # str | None
    device_chosen = pyqtSignal(object)            # str | None (None = auto)

    BOTTOM_TOLERANCE = 4    # px: "at the bottom" if the scrollbar is within this distance

    def __init__(self) -> None:
        super().__init__()
        self._drag_offset = None
        self._devices: List[str] = []
        self._device_hint: Optional[str] = None
        self._font_size = config.FONT_SIZE_DEFAULT
        self._settings: Optional[SettingsDialog] = None

        self.setWindowTitle("Live Translator")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)  # don't steal focus from the game
        self.resize(460, 260)
        self._build_ui()
        self._move_to_corner()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QtWidgets.QFrame()
        panel.setObjectName("panel")
        panel.setStyleSheet(STYLE)
        outer.addWidget(panel)

        lay = QtWidgets.QVBoxLayout(panel)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        # Header: status text + settings gear + close. Drag the window by this bar.
        header = QtWidgets.QHBoxLayout()
        self._status = QtWidgets.QLabel("Starting...")
        gear = QtWidgets.QPushButton("\u2699")      # change to "Set" if your font lacks the glyph
        gear.setObjectName("gear")
        gear.setToolTip("Settings")
        gear.setFixedSize(24, 24)
        gear.clicked.connect(self._toggle_settings)
        close_btn = QtWidgets.QPushButton("\u2715")
        close_btn.setObjectName("close")
        close_btn.setFixedSize(22, 22)
        close_btn.clicked.connect(QtWidgets.QApplication.quit)
        header.addWidget(self._status, 1)
        header.addWidget(gear)
        header.addWidget(close_btn)
        lay.addLayout(header)

        # Scrollable, transparent chat history
        self._log = QtWidgets.QTextBrowser()
        self._log.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._log.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._log.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._log.setOpenLinks(False)
        self._log.setContextMenuPolicy(Qt.NoContextMenu)   # let our right-click menu handle it
        self._log.viewport().setAutoFillBackground(False)
        self._log.setMinimumHeight(60)
        self._log.document().setMaximumBlockCount(config.HISTORY_MAX_BLOCKS)
        self._apply_font_size(self._font_size)
        lay.addWidget(self._log, 1)

        row = QtWidgets.QHBoxLayout()
        self._input = QtWidgets.QLineEdit()
        self._input.setPlaceholderText("Scrie in rom\u00e2n\u0103 \u2192 Enter trimite \u00een englez\u0103 \u00een joc")
        self._input.returnPressed.connect(self._on_enter)
        row.addWidget(self._input, 1)
        row.addWidget(QtWidgets.QSizeGrip(panel), 0, Qt.AlignBottom | Qt.AlignRight)
        lay.addLayout(row)

    def _move_to_corner(self) -> None:
        geo = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.move(geo.right() - self.width() - 20, geo.top() + 20)

    # ------------------------------------------------------------------ chat history
    def _at_bottom(self) -> bool:
        bar = self._log.verticalScrollBar()
        return bar.value() >= bar.maximum() - self.BOTTOM_TOLERANCE

    def _scroll_to_bottom(self) -> None:
        bar = self._log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _append(self, html_block: str) -> None:
        """Append one entry. Auto-scroll only if the user is already at the bottom;
        if they scrolled up to read history, keep their position."""
        bar = self._log.verticalScrollBar()
        old_value = bar.value()
        stick = self._at_bottom()
        self._log.append(f"<div style='margin-bottom:6px'>{html_block}</div>")
        bar.setValue(bar.maximum() if stick else old_value)

    def _apply_font_size(self, pt: int) -> None:
        """Instantly resize all text in the history (past entries included)."""
        self._font_size = pt
        stick = self._at_bottom()
        font = QtGui.QFont(self._log.font())
        font.setPointSize(pt)
        self._log.setFont(font)
        self._log.document().setDefaultFont(font)   # rich-text content follows the document font
        if stick:   # layout is refreshed asynchronously, so scroll on the next event-loop turn
            QtCore.QTimer.singleShot(0, self._scroll_to_bottom)

    # ------------------------------------------------------------------ slots (GUI thread)
    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_devices(self, names: List[str]) -> None:
        self._devices = names

    def add_incoming(self, lang: str, original: str, translated: str) -> None:
        self._append(
            f"<span style='color:#8b95a5'><small>[{html.escape(lang)}] {html.escape(original)}</small></span>"
            f"<br><b>{html.escape(translated)}</b>")

    def add_outgoing(self, romanian: str, english: str) -> None:
        self._append(
            f"<span style='color:#7fd1ff'><small>\u2192 {html.escape(romanian)}</small><br>"
            f"<b>{html.escape(english)}</b></span>")

    def _on_enter(self) -> None:
        text = self._input.text().strip()
        if text:
            self.text_submitted.emit(text)
            self._input.clear()

    # ------------------------------------------------------------------ settings dialog
    def _toggle_settings(self) -> None:
        if self._settings is None:
            self._settings = SettingsDialog(self, self._font_size)
            self._settings.source_language_changed.connect(self.source_language_changed)  # forward to backend
            self._settings.font_size_changed.connect(self._apply_font_size)
        dlg = self._settings
        if dlg.isVisible():
            dlg.hide()
            return
        dlg.adjustSize()
        self._position_dialog(dlg)
        dlg.show()

    def _position_dialog(self, dlg: QtWidgets.QDialog) -> None:
        """Place the dialog under the overlay (or above it if there's no room), inside the screen."""
        screen = QtWidgets.QApplication.screenAt(self.geometry().center()) or QtWidgets.QApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = self.x() + self.width() - dlg.width()
        x = max(geo.left(), min(x, geo.right() - dlg.width()))
        y = self.y() + self.height() + 6
        if y + dlg.height() > geo.bottom():
            y = max(geo.top(), self.y() - dlg.height() - 6)
        dlg.move(x, y)

    # ------------------------------------------------------------------ dragging (use the header bar)
    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._drag_offset = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag_offset is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPos() - self._drag_offset)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        self._drag_offset = None

    # ------------------------------------------------------------------ right-click menu
    def contextMenuEvent(self, e) -> None:  # noqa: N802
        menu = QtWidgets.QMenu(self)
        settings = menu.addAction("Settings...")
        menu.addSeparator()
        dev_menu = menu.addMenu("Audio device (loopback)")
        actions = {dev_menu.addAction("Auto (default output)"): None}
        for name in self._devices:
            actions[dev_menu.addAction(name)] = name
        rescan = menu.addAction("Rescan devices")
        clear = menu.addAction("Clear history")
        menu.addSeparator()
        quit_ = menu.addAction("Quit")

        chosen = menu.exec_(e.globalPos())
        if chosen in actions:
            self._device_hint = actions[chosen]
            self.device_chosen.emit(self._device_hint)
        elif chosen is settings:
            self._toggle_settings()
        elif chosen is rescan:
            self.device_chosen.emit(self._device_hint)
        elif chosen is clear:
            self._log.clear()
        elif chosen is quit_:
            QtWidgets.QApplication.quit()
