"""Presença: volume de luz entrelaçado, ligado aos estados existentes."""
from __future__ import annotations

import math
import random
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient
from PySide6.QtWidgets import QWidget

from app.core.state import STATE_LABELS, State
from app.ui.theme import MUTED, ORB_COLORS, display_font

ROT_SPEED = {State.IDLE: 9, State.ACTIVATED: 70, State.LISTENING: 24,
             State.PROCESSING: 230, State.SPEAKING: 34, State.ERROR: 3}
AA = QPainter.RenderHint.Antialiasing


class CoreWidget(QWidget):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(280, 260)
        self.state = State.IDLE
        self.level = 0.0
        self._target = 0.0
        self._rot = 0.0
        self._flash = 0.0
        self._banner = ""
        self._banner_until = 0.0
        self._idle_hint = ""
        self._presentation_only = False
        c = QColor(ORB_COLORS[State.IDLE.value])
        self._cur = [float(c.red()), float(c.green()), float(c.blue())]
        self._t0 = self._last = time.perf_counter()
        rnd = random.Random(7)
        self._particles = [(rnd.uniform(0, 6.283), rnd.uniform(1.25, 2.0), rnd.uniform(-0.25, 0.25),
                            rnd.uniform(1.0, 2.2), rnd.uniform(0, 6.283)) for _ in range(34)]
        # Curva tridimensional contínua, compartilhada por todos os estados visuais.
        self._ribbon = []
        for i in range(121):
            u = math.tau * i / 120
            radius = 1.70 + 0.66 * math.cos(3 * u)
            self._ribbon.append((radius * math.cos(2 * u), radius * math.sin(2 * u),
                                 0.92 * math.sin(3 * u), u))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ---- API ---------------------------------------------------------------
    def set_state(self, state: State) -> None:
        if state == State.ACTIVATED:
            self._flash = 1.0
        self.state = state

    def set_level(self, v: float) -> None:
        self._target = max(0.0, min(1.0, v))

    def set_idle_hint(self, text: str) -> None:
        """Linha discreta em standby, ex.: DIGA "APOLO" OU BATA DUAS PALMAS."""
        self._idle_hint = text

    def show_banner(self, text: str, ms: int = 1500) -> None:
        self._banner = text
        self._banner_until = time.perf_counter() + ms / 1000

    # ---- ciclo -----------------------------------------------------------
    def showEvent(self, e):
        self._last = time.perf_counter()
        self._timer.start(50)  # mantém a animação suave sem disputar tanta CPU com a voz
        super().showEvent(e)

    def hideEvent(self, e):  # janela na bandeja: para de animar (economiza CPU)
        self._timer.stop()
        super().hideEvent(e)

    def mousePressEvent(self, e):
        c = self.rect().center()
        if (e.position().x() - c.x()) ** 2 + (e.position().y() - c.y()) ** 2 < (min(self.width(), self.height()) * 0.32) ** 2:
            self.clicked.emit()

    def _tick(self) -> None:
        now = time.perf_counter()
        dt = min(0.1, now - self._last)
        self._last = now
        self.level += (self._target - self.level) * min(1.0, dt * 14)
        self._target *= max(0.0, 1.0 - dt * 5)  # decai se ninguém atualizar
        self._rot = (self._rot + ROT_SPEED[self.state] * dt) % 3600
        self._flash = max(0.0, self._flash - dt * 0.9)
        goal = QColor(ORB_COLORS[self.state.value])
        for i, v in enumerate((goal.red(), goal.green(), goal.blue())):
            self._cur[i] += (v - self._cur[i]) * min(1.0, dt * 6)
        self.update()

    # ---- desenho -----------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(AA)
        w, h = self.width(), self.height()
        cx, cy = w * 0.50, (h - 76) * 0.50
        R = max(28, min(w * 0.24, (h - 105) * 0.30))
        t = time.perf_counter() - self._t0
        st, lvl = self.state, self.level
        energy = lvl if st in (State.LISTENING, State.SPEAKING) else 0
        pulse = 1 + 0.026 * math.sin(t * 1.7) + energy * 0.08 + self._flash * 0.06
        base = QColor(*(int(v) for v in self._cur))
        no_pen, no_brush = Qt.PenStyle.NoPen, Qt.BrushStyle.NoBrush

        def tint(alpha):
            c = QColor(base)
            c.setAlphaF(max(0, min(1, alpha)))
            return c

        # Identificação discreta da presença.
        p.setFont(display_font(8, QFont.Weight.Medium, spacing=2.5))
        p.setPen(QColor('#a28b88'))
        p.drawText(QRectF(2, 0, w - 4, 22), Qt.AlignmentFlag.AlignRight, '02  /  PRESENÇA')

        halo = QRadialGradient(QPointF(cx, cy), R * 2.2)
        halo.setColorAt(0, tint(0.20 + energy * 0.10))
        halo.setColorAt(0.5, tint(0.06))
        halo.setColorAt(1, tint(0))
        p.setPen(no_pen)
        p.setBrush(halo)
        p.drawEllipse(QPointF(cx, cy), R * 2.2, R * 2.2)

        # Pequenos fragmentos livres deixam uma trilha em vez de órbitas circulares.
        for a0, rf, speed, size, phase in self._particles:
            angle = a0 + t * speed * 0.12
            x = cx + math.cos(angle) * R * rf * 1.03
            y = cy + math.sin(angle) * R * rf * 0.68
            p.setPen(QPen(tint(0.16 + 0.20 * (0.5 + 0.5 * math.sin(t + phase))), 1))
            p.drawLine(QPointF(x, y), QPointF(x + size * 2.5, y - size * 1.2))

        # Trevo tridimensional: uma só fita volumétrica que se cruza por trás e pela frente.
        # A ordenação de profundidade mantém as dobras legíveis sem usar uma engine 3D.
        spin = math.radians(self._rot * 0.26) + 0.48
        co, si = math.cos(spin), math.sin(spin)
        tilt = 0.67 + 0.08 * math.sin(t * 0.24)
        ct, ss = math.cos(tilt), math.sin(tilt)
        vertices = []
        for x, y, z, u in self._ribbon:
            xx, zz = x * co + z * si, z * co - x * si
            yy, zz = y * ct - zz * ss, y * ss + zz * ct
            deform = 1 + energy * 0.045 * math.sin(u * 8 + t * 3)
            perspective = 1 + zz * 0.035
            pos = QPointF(cx + xx * R * 0.64 * pulse * deform * perspective,
                          cy + yy * R * 0.64 * pulse * deform * perspective)
            vertices.append((pos, zz, u))
        # Reserva margem para a legenda mesmo nas orientações mais abertas.
        top = min(v[0].y() for v in vertices) - R * 0.14
        bottom = max(v[0].y() for v in vertices) + R * 0.14
        fit = min(1.0, (cy - 30) / max(1, cy - top),
                  (h - 95 - cy) / max(1, bottom - cy))
        if fit < 1:
            vertices = [(QPointF(cx + (pos.x() - cx) * fit, cy + (pos.y() - cy) * fit), z, u)
                        for pos, z, u in vertices]
            R *= fit
        # Traços contínuos, suavizados por curvas quadráticas: sem emendas na superfície.
        chunks = [(i, min(i + 10, 120)) for i in range(0, 120, 10)]
        chunks.sort(key=lambda chunk: sum(vertices[j][1] for j in range(chunk[0], chunk[1] + 1)) / (chunk[1] - chunk[0] + 1))
        material = QRadialGradient(QPointF(cx - R * 0.35, cy - R * 0.75), R * 2.2)
        material.setColorAt(0, QColor('#ffad79'))
        material.setColorAt(0.26, QColor('#ff371e'))
        material.setColorAt(0.64, QColor('#b71013'))
        material.setColorAt(1, QColor('#430206'))
        p.setBrush(no_brush)

        def smooth_path(points):
            path = QPainterPath(points[0])
            for j in range(1, len(points) - 1):
                mid = QPointF((points[j].x() + points[j + 1].x()) / 2,
                              (points[j].y() + points[j + 1].y()) / 2)
                path.quadTo(points[j], mid)
            path.lineTo(points[-1])
            return path

        width = R * 0.21
        for first, last in chunks:
            points = [vertices[j][0] for j in range(first, last + 1)]
            depth = max(0, min(1, (sum(vertices[j][1] for j in range(first, last + 1)) / len(points) + 2.25) / 4.5))
            path = smooth_path(points)
            pen = QPen(QBrush(material), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPath(path)
            # Reflexo deslocado sobre a fita, acompanhando a tangente de cada dobra.
            highlights = []
            for j in range(first, last + 1):
                prev = vertices[max(0, j - 1)][0]
                nxt = vertices[min(120, j + 1)][0]
                dx, dy = nxt.x() - prev.x(), nxt.y() - prev.y()
                length = max(0.1, math.hypot(dx, dy))
                pos = vertices[j][0]
                highlights.append(QPointF(pos.x() - dy / length * width * 0.21,
                                          pos.y() + dx / length * width * 0.21))
            shine = QColor(255, 161, 107, int(28 + depth * 87))
            p.setPen(QPen(shine, width * 0.18, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPath(smooth_path(highlights))

        # Uma sombra luminosa ancora a presença ao espaço, sem moldura ou disco.
        floor = QPointF(cx, cy + R * 1.52)
        p.save()
        p.translate(floor)
        p.scale(1, 0.13)
        pool = QRadialGradient(QPointF(0, 0), R * 0.86)
        pool.setColorAt(0, tint(0.20 + energy * 0.12))
        pool.setColorAt(1, tint(0))
        p.setBrush(pool)
        p.setPen(no_pen)
        p.drawEllipse(QPointF(0, 0), R * 0.86, R * 0.86)
        p.restore()

        if self._presentation_only:
            p.end()
            return

        # Legenda ligada aos estados e banners já existentes.
        now = time.perf_counter()
        banner = self._banner and now < self._banner_until
        text = self._banner if banner else STATE_LABELS[st]
        p.setFont(display_font(8.5, QFont.Weight.Medium, spacing=1.6))
        p.setPen(QColor('#ff6254'))
        line = p.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, w - 42)
        p.drawText(QRectF(20, h - 60, w - 40, 26), Qt.AlignmentFlag.AlignCenter, '•  ' + line)
        if st == State.IDLE and self._idle_hint and not banner:
            p.setFont(display_font(7.5, spacing=1))
            p.setPen(QColor('#9e8786'))
            hint = p.fontMetrics().elidedText(self._idle_hint, Qt.TextElideMode.ElideRight, w - 24)
            p.drawText(QRectF(12, h - 29, w - 24, 20), Qt.AlignmentFlag.AlignCenter, hint)
        p.end()
