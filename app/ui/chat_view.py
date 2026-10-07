from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget

from app.ui.theme import ACCENT, ERR, MUTED, TEXT

ROLES = {"you": ("VOCÊ", MUTED, TEXT), "apolo": ("APOLO", ACCENT, TEXT), "system": ("AVISO", ERR, "#ffb3c0")}


class Bubble(QFrame):
    def __init__(self, role: str, text: str):
        super().__init__()
        name, bar, fg = ROLES.get(role, ROLES["system"])
        self.setObjectName("bubble")
        self.setStyleSheet("QFrame#bubble { border:none; border-bottom:1px solid #3b2822; border-radius:0; background:transparent; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 16, 8, 18)
        lay.setSpacing(8)
        head = QLabel(name)
        head.setStyleSheet(f"color: {bar}; font-size: 11px; font-weight: 600; letter-spacing: 2px;")
        self.body = QLabel(text)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setStyleSheet(f"color: {fg}; font-size: 15px; background: transparent;")
        lay.addWidget(head)
        lay.addWidget(self.body)


class ChatView(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        self._lay = QVBoxLayout(inner)
        self._lay.setContentsMargins(4, 4, 8, 4)
        self._lay.setSpacing(14)
        self._lay.addStretch(1)
        self.setWidget(inner)
        self._last: Bubble | None = None
        self._empty = QLabel('<div style="color:#ff4a2a; font-size:54px;">“</div>'
                             '<div style="color:#f2e5d9; font-size:34px; font-weight:300;">Ideias.<br>Perguntas.<br>Possibilidades.</div>'
                             '<br><div style="color:#b39889; font-size:13px;">Sua próxima conversa<br>pode começar por qualquer lugar.</div>')
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._empty.setStyleSheet(f"color: {MUTED}; padding: 12px 0;")
        self._lay.insertWidget(0, self._empty)

    def add(self, role: str, text: str) -> None:
        self._empty.hide()
        b = Bubble(role, text)
        self._lay.insertWidget(self._lay.count() - 1, b)
        if role == "apolo":
            self._last = b
        self._scroll()

    def update_last(self, text: str) -> None:
        if self._last is not None:
            follow = self.verticalScrollBar().maximum() - self.verticalScrollBar().value() < 80
            self._last.body.setText(text)
            if follow:
                self._scroll()

    def clear(self) -> None:
        while self._lay.count() > 2:
            item = self._lay.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
        self._last = None
        self._empty.show()

    def _scroll(self) -> None:
        bar = self.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))
