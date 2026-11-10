"""overlay.py - frameless, translucent, always-on-top PyQt5 window (UI only, no heavy work)."""
from __future__ import annotations

import html
from typing import List, Optional

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal

import config

STYLE = """
#panel { background: rgba(14, 16, 22, 190); border: 1px solid rgba(255,255,255,45);
         border-radius: 12px; }
QLabel { color: #9aa4b2; font-size: 11px; }
QTextEdit { background: transparent; border: none; color: #ffffff; font-size: 14px; }
QLineEdit { background: rgba(255,255,255,28); border: 1px solid rgba(255,255,255,55);
            border-radius: 8px; color: #ffffff; padding: 6px 8px; font-size: 14px; }
QComboBox { background: rgba(255,255,255,28); color: #fff; border: 1px solid rgba(255,255,255,45);
            border-radius: 6px; padding: 1px 6px; font-size: 11px; }
QComboBox QAbstractItemView { background: #1b1e27; color: #fff; selection-background-color: #3b4a6b; }
QPushButton { background: transparent; color: #cbd2dc; border: none; font-size: 13px; }
QPushButton:hover { color: #ff6b6b; }
"""


class OverlayWindow(QtWidgets.QWidget):
    text_submitted = pyqtSignal(str)             # Romanian text typed by the user
    source_language_changed = pyqtSignal(object)  # str | None
    device_chosen = pyqtSignal(object)            # str | None (None = auto)

    def __init__(self) -> None:
        super().__init__()
        self._drag_offset = None
        self._devices: List[str] = []
        self._device_hint: Optional[str] = None

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

        header = QtWidgets.QHBoxLayout()
        self._status = QtWidgets.QLabel("Starting...")
        self._lang = QtWidgets.QComboBox()
        for label, code in config.SOURCE_LANGUAGES.items():
            self._lang.addItem(label, code)
        self._lang.currentIndexChanged.connect(
            lambda _i: self.source_language_changed.emit(self._lang.currentData()))
        close_btn = QtWidgets.QPushButton("\u2715")
        close_btn.setFixedSize(22, 22)
        close_btn.clicked.connect(QtWidgets.QApplication.quit)
        header.addWidget(self._status, 1)
        header.addWidget(self._lang)
        header.addWidget(close_btn)
        lay.addLayout(header)

        self._log = QtWidgets.QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._log.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._log.setAttribute(Qt.WA_TransparentForMouseEvents, True)  # lets you drag the window
        self._log.document().setMaximumBlockCount(80)
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

    # ------------------------------------------------------------------ slots (GUI thread)
    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_devices(self, names: List[str]) -> None:
        self._devices = names

    def add_incoming(self, lang: str, original: str, translated: str) -> None:
        self._append(
            f"<span style='color:#8b95a5;font-size:11px'>[{html.escape(lang)}] {html.escape(original)}</span>"
            f"<br><b>{html.escape(translated)}</b>")

    def add_outgoing(self, romanian: str, english: str) -> None:
        self._append(
            f"<span style='color:#7fd1ff;font-size:11px'>\u2192 {html.escape(romanian)}</span>"
            f"<br><b style='color:#7fd1ff'>{html.escape(english)}</b>")

    def _append(self, html_block: str) -> None:
        self._log.append(f"<div style='margin-bottom:6px'>{html_block}</div>")
        bar = self._log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_enter(self) -> None:
        text = self._input.text().strip()
        if text:
            self.text_submitted.emit(text)
            self._input.clear()

    # ------------------------------------------------------------------ dragging
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
        dev_menu = menu.addMenu("Audio device (loopback)")
        actions = {dev_menu.addAction("Auto (default output)"): None}
        for name in self._devices:
            actions[dev_menu.addAction(name)] = name
        menu.addSeparator()
        rescan = menu.addAction("Rescan devices")
        clear = menu.addAction("Clear text")
        quit_ = menu.addAction("Quit")

        chosen = menu.exec_(e.globalPos())
        if chosen in actions:
            self._device_hint = actions[chosen]
            self.device_chosen.emit(self._device_hint)
        elif chosen is rescan:
            self.device_chosen.emit(self._device_hint)
        elif chosen is clear:
            self._log.clear()
        elif chosen is quit_:
            QtWidgets.QApplication.quit()
