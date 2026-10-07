"""Marca da presença, desenhada sem depender de imagens externas."""
import math
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap


def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor('#100b0c'))
    p.drawRoundedRect(1, 1, 62, 62, 15, 15)
    path = QPainterPath()
    for i in range(181):
        u = i * math.tau / 180
        r = 1.7 + .66 * math.cos(3*u)
        x, y, z = r*math.cos(2*u), r*math.sin(2*u), .92*math.sin(3*u)
        xx, zz = x*math.cos(.48)+z*math.sin(.48), z*math.cos(.48)-x*math.sin(.48)
        yy = y*math.cos(.67)-zz*math.sin(.67)
        pt = QPointF(32+xx*10.5, 32+yy*10.5)
        path.moveTo(pt) if i==0 else path.lineTo(pt)
    gradient = QLinearGradient(12, 5, 52, 59)
    gradient.setColorAt(0, QColor('#ffb17a'))
    gradient.setColorAt(.40, QColor('#ff3822'))
    gradient.setColorAt(1, QColor('#a31611'))
    p.setBrush(Qt.BrushStyle.NoBrush)
    pen = QPen(gradient, 5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawPath(path)
    p.end()
    return QIcon(pm)
