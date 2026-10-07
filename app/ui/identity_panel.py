"""Peça visual reutilizável nas telas de entrada, perfil e tutorial."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

from app.ui.core_widget import CoreWidget


class IdentityPanel(QFrame):
    def __init__(self, caption="SEU UNIVERSO PESSOAL", parent=None):
        super().__init__(parent)
        self.setObjectName("identityPanel")
        self.setFixedWidth(260)
        self.setStyleSheet("QFrame#identityPanel {background:#190e0d; border:none; border-left:3px solid #ff4023;}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 24, 20, 24)
        layout.setSpacing(16)
        brand = QLabel("apolo.")
        brand.setStyleSheet("font-size:38px; font-weight:600; color:#fff0e2;")
        layout.addWidget(brand)
        tag = QLabel(caption)
        tag.setWordWrap(True)
        tag.setStyleSheet("font-size:9px; color:#fb7d54;")
        layout.addWidget(tag)
        self.orb = CoreWidget()
        self.orb.setMinimumSize(200, 260)
        self.orb._presentation_only = True
        self.orb.setCursor(Qt.CursorShape.ArrowCursor)
        layout.addWidget(self.orb, 1)
        title = QLabel("Uma presença.\nInfinitas\npossibilidades.")
        title.setStyleSheet("font-size:25px; font-weight:300; color:#f9d5bb;")
        layout.addWidget(title)
        detail = QLabel("VOZ  /  IDEIAS  /  CONVERSA")
        detail.setStyleSheet("font-size:8px; color:#b26b4f;")
        layout.addWidget(detail)
