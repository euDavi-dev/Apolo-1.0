"""Composição de uma conversa no WhatsApp, sem envio automático."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout)

from app.services.whatsapp_service import WhatsAppError, chat_url


class WhatsAppDialog(QDialog):
    def __init__(self, assistant, parent=None):
        super().__init__(parent)
        self.a = assistant
        self.setWindowTitle("WhatsApp · Apolo")
        self.resize(580, 460)
        self.setMinimumSize(420, 400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        title = QLabel("Uma conversa, pronta para começar.")
        title.setObjectName("title")
        title.setWordWrap(True)
        layout.addWidget(title)
        hint = QLabel("Abra o WhatsApp Web ou prepare uma mensagem para um telefone. "
                      "Na primeira conexão, vincule sua conta pelo QR code do WhatsApp.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.phone = QLineEdit()
        self.phone.setPlaceholderText("+55 11 99999-9999 (DDI + DDD + número)")
        self.phone.setMaxLength(40)
        form.addRow("Telefone", self.phone)
        self.message = QPlainTextEdit()
        self.message.setPlaceholderText("Escreva a mensagem que deseja preparar…")
        self.message.setStyleSheet("background:#151214; border:1px solid #624235; border-radius:5px; padding:9px;")
        self.message.setMinimumHeight(110)
        form.addRow("Mensagem", self.message)
        layout.addLayout(form, 1)
        review = QLabel("A mensagem será preenchida na conversa. Revise e clique em Enviar no WhatsApp.")
        review.setObjectName("muted")
        review.setWordWrap(True)
        layout.addWidget(review)
        self.status = QLabel("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        for label, slot in (("WhatsApp Web", self._web), ("Copiar link", self._copy),
                            ("Preparar conversa", self._prepare)):
            button = QPushButton(label)
            if label == "Preparar conversa":
                button.setObjectName("primary")
            button.clicked.connect(slot)
            actions.addWidget(button)
        layout.addLayout(actions)

    def _web(self):
        try:
            self.status.setText(self.a.whatsapp.open_chat())
        except WhatsAppError as exc:
            self.status.setText(str(exc))

    def _prepare(self):
        if not self.phone.text().strip():
            self.status.setText("Informe o telefone do destinatário, com DDI e DDD.")
            self.phone.setFocus()
            return
        try:
            self.status.setText(self.a.whatsapp.open_chat(self.phone.text(), self.message.toPlainText()))
        except WhatsAppError as exc:
            self.status.setText(str(exc))

    def _copy(self):
        try:
            url = chat_url(self.phone.text(), self.message.toPlainText())
        except WhatsAppError as exc:
            self.status.setText(str(exc))
            return
        QApplication.clipboard().setText(url)
        self.status.setText("Link copiado. Ele contém o telefone e a mensagem preparada.")
