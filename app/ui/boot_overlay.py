"""Animação de inicialização: as linhas aparecem conforme os módulos realmente ficam online."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QVBoxLayout, QWidget

from app.ui.theme import ACCENT, ERR, MUTED, OK, display_font, mono_font
from app.ui.core_widget import CoreWidget
from app.core.state import State


class BootOverlay(QWidget):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("boot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("#boot { background: #0b0b0d; }")
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("apolo.")
        title.setFont(display_font(38, spacing=-2))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lines = QLabel("")
        self.lines.setFont(mono_font(10.5))
        self.lines.setMinimumWidth(320)
        self.lines.setMinimumHeight(190)
        self.lines.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        brand = QLabel("UMA PRESENÇA ESTÁ DESPERTANDO")
        brand.setStyleSheet(f"color: {ACCENT}; font-size: 10px;")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        orb = CoreWidget(self)
        orb.set_state(State.PROCESSING)
        orb.setFixedSize(400, 340)
        orb._presentation_only = True
        orb.setCursor(Qt.CursorShape.ArrowCursor)
        lay.addWidget(brand)
        lay.addWidget(orb, alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)
        lay.addSpacing(20)
        lay.addWidget(self.lines, alignment=Qt.AlignmentFlag.AlignCenter)
        self._queue: list[str] = []
        self._shown: list[str] = []
        self._ready = False
        self._timer = QTimer(self)
        self._timer.setInterval(240)
        self._timer.timeout.connect(self._step)
        self._fx = QGraphicsOpacityEffect(self)
        self._fx.setOpacity(1.0)
        self.setGraphicsEffect(self._fx)
        self._anim = None
        parent.installEventFilter(self)
        self.setGeometry(parent.rect())

    def eventFilter(self, obj, ev):
        if obj is self.parent() and ev.type() == QEvent.Type.Resize:
            self.setGeometry(self.parent().rect())
        return False

    def add(self, line: str) -> None:
        self._queue.append(line)
        if not self._timer.isActive():
            self._timer.start()

    def ready(self) -> None:
        self._ready = True
        if not self._timer.isActive():
            self._timer.start()

    def _step(self) -> None:
        if self._queue:
            self._shown.append(self._queue.pop(0))
            self._render()
        elif self._ready:
            self._timer.stop()
            QTimer.singleShot(650, self._fade)
        else:
            self._timer.stop()

    def _render(self) -> None:
        html = []
        for ln in self._shown[-8:]:
            color = ERR if "OFFLINE" in ln else OK if ("ONLINE" in ln or "READY" in ln) else MUTED
            html.append(f'<span style="color:{color}">{ln}</span>')
        self.lines.setText("<br>".join(html))

    def _fade(self) -> None:
        self._anim = QPropertyAnimation(self._fx, b"opacity", self)
        self._anim.setDuration(500)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self.hide)
        self._anim.start()
