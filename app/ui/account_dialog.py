from __future__ import annotations

from copy import deepcopy
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget, QWizard, QWizardPage)

from app.core.accounts import AccountError, AccountStore, Session, validate_profile
from app.ui.identity_panel import IdentityPanel


def paragraph(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def entry(placeholder: str, secret=False) -> QLineEdit:
    field = QLineEdit()
    field.setPlaceholderText(placeholder)
    field.setMaxLength(256 if secret else 100)
    if secret:
        field.setEchoMode(QLineEdit.EchoMode.Password)
    return field


class LoginDialog(QDialog):
    def __init__(self, store: AccountStore, parent=None):
        super().__init__(parent)
        self.store, self.session = store, None
        self.signup = not store.has_accounts()
        self.setWindowTitle("APOLO · Sua central pessoal")
        self.setMinimumWidth(900)
        self.resize(940, 720)
        shell = QHBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(IdentityPanel("PRIMEIRO CONTATO"))
        content = QWidget()
        shell.addWidget(content, 1)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(14)
        brand = paragraph("01  /  BEM-VINDO AO SEU ESPAÇO")
        brand.setObjectName("eyebrow")
        layout.addWidget(brand)
        self.title = paragraph("")
        self.title.setObjectName("heroTitle")
        layout.addWidget(self.title)
        layout.addWidget(paragraph("Sou seu assistente pessoal. Posso conversar, informar o clima e abrir aplicativos. "
                                   "Primeiro, vamos preparar sua conta; depois mostro como me chamar."))
        self.username = entry("Ex.: ana.silva")
        self.password = entry("Pelo menos 10 caracteres", True)
        form = QFormLayout()
        form.addRow("Usuário", self.username)
        form.addRow("Senha", self.password)
        layout.addLayout(form)
        self.registration = QWidget()
        extra = QFormLayout(self.registration)
        extra.setContentsMargins(0, 0, 0, 0)
        self.name = entry("Como posso chamar você?")
        self.city = entry("Sua cidade para a previsão do tempo")
        self.confirm = entry("Repita a senha", True)
        extra.addRow("Seu nome", self.name)
        extra.addRow("Cidade", self.city)
        extra.addRow("Confirmar senha", self.confirm)
        for group in (form, extra):
            for row in range(group.rowCount()):
                group.itemAt(row, QFormLayout.ItemRole.LabelRole).widget().setFixedWidth(100)
        layout.addWidget(self.registration)
        show = QCheckBox("Mostrar senha")
        show.toggled.connect(lambda visible: [field.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
                                             for field in (self.password, self.confirm)])
        layout.addWidget(show)
        layout.addWidget(paragraph("Conta local neste computador, sem e-mail ou sincronização. Guarde sua senha: "
                                   "ela protege seu perfil e suas chaves, e não há recuperação por e-mail."))
        self.error = paragraph("")
        self.error.setStyleSheet("color: #ffb347")
        layout.addWidget(self.error)
        self.submit = QPushButton()
        self.submit.setObjectName("primary")
        self.submit.clicked.connect(self.authenticate)
        self.submit.setDefault(True)
        layout.addWidget(self.submit)
        self.switch = QPushButton()
        self.switch.clicked.connect(self.toggle)
        layout.addWidget(self.switch)
        self.refresh()

    def refresh(self):
        self.registration.setVisible(self.signup)
        self.title.setText("Vamos nos conhecer?" if self.signup else "Bom ter você de volta.")
        self.submit.setText("Criar minha conta" if self.signup else "Entrar no Apolo")
        self.switch.setText("Já tenho uma conta" if self.signup else "Criar outra conta")
        self.error.clear()

    def toggle(self):
        self.signup = not self.signup
        self.password.clear()
        self.confirm.clear()
        self.refresh()

    def authenticate(self):
        try:
            if self.signup:
                if self.password.text() != self.confirm.text():
                    raise AccountError("As senhas não coincidem. Digite novamente.")
                self.session = self.store.create(self.username.text(), self.password.text(), self.name.text(), self.city.text())
            else:
                self.session = self.store.login(self.username.text(), self.password.text())
        except (AccountError, OSError) as exc:
            self.error.setText(str(exc) if isinstance(exc, AccountError) else "Não foi possível acessar os dados da conta. Verifique as permissões da pasta.")
            self.submit.setEnabled(False)
            QTimer.singleShot(1000, lambda: self.submit.setEnabled(True))
            return
        self.password.clear()
        self.confirm.clear()
        self.accept()


class ProfileForm(QWidget):
    def __init__(self, session: Session):
        super().__init__()
        form = QFormLayout(self)
        self.name, self.city = entry("Seu nome"), entry("Sua cidade")
        self.name.setText(session.data["name"])
        self.city.setText(session.data["city"])
        form.addRow("Nome", self.name)
        form.addRow("Cidade", self.city)
        self.keys = {}
        for key, label in (("GEMINI_API_KEY", "Gemini · conversas com IA"),
                           ("YOUTUBE_API_KEY", "YouTube · opcional"), ("SPOTIFY_CLIENT_ID", "Spotify Client ID · opcional")):
            field = entry("Cole aqui ou configure depois", True)
            field.setMaxLength(4096)
            field.setText(session.data["secrets"].get(key, ""))
            form.addRow(label, field)
            self.keys[key] = field
        show = QCheckBox("Mostrar chaves")
        show.toggled.connect(lambda visible: [f.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
                                             for f in self.keys.values()])
        form.addRow(show)
        form.addRow(paragraph("As chaves são criptografadas com sua senha. Deixe um campo vazio para remover a chave. "
                              "Sem Gemini, os comandos locais continuam disponíveis. A validade é conferida quando o serviço é usado."))
        form.addRow(paragraph("Use uma chave do Google AI Studio para Gemini e do Google Cloud para YouTube Data API. "
                              "Spotify precisa também da autorização OAuth descrita no README. As contas e cotas dos provedores são independentes."))

    def values(self, session: Session) -> dict:
        validate_profile(self.name.text(), self.city.text())
        data = deepcopy(session.data)
        data.update(name=self.name.text().strip(), city=self.city.text().strip(),
                    secrets={k: f.text().strip() for k, f in self.keys.items()})
        if any(any(c.isspace() for c in value) for value in data["secrets"].values()):
            raise AccountError("Uma chave contém espaços ou quebras de linha. Cole somente a chave.")
        return data


class TutorialWizard(QWizard):
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("APOLO · Preparando sua central")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self._identity = IdentityPanel("GUIA DE PRIMEIROS PASSOS")
        self.setSideWidget(self._identity)
        banner = QPixmap(980, 74)
        painter = QPainter(banner)
        gradient = QLinearGradient(0, 0, 980, 0)
        gradient.setColorAt(0, QColor("#29130e"))
        gradient.setColorAt(1, QColor("#0b0b0d"))
        painter.fillRect(banner.rect(), gradient)
        painter.end()
        self.setPixmap(QWizard.WizardPixmap.BannerPixmap, banner)
        self.resize(980, 680)
        for button, label in ((QWizard.WizardButton.NextButton, "Continuar →"),
                              (QWizard.WizardButton.BackButton, "← Voltar"),
                              (QWizard.WizardButton.FinishButton, "Começar a usar"),
                              (QWizard.WizardButton.CancelButton, "Continuar depois")):
            self.setButtonText(button, label)
        self.profile = ProfileForm(session)
        page = QWizardPage()
        page.setTitle("01 / Sua central, do seu jeito")
        page.setSubTitle("Confira seu perfil e conecte os serviços que deseja usar.")
        layout = QVBoxLayout(page)
        layout.addWidget(self.profile)
        self.addPage(page)
        for title, subtitle, text in (
            ("02 / Pode me chamar", "Três caminhos para a mesma conversa.",
             "VOZ  ·  Diga ‘Apolo’, espere o sinal e faça seu pedido. Você também pode dizer ‘Apolo, que horas são?’ de uma vez.\n\n"
             "TOQUE  ·  Clique no núcleo central ou use Ctrl+Espaço para falar. Duas palmas também ativam a escuta quando habilitadas.\n\n"
             "TEXTO  ·  Digite na conversa e pressione Enter. Ctrl+L leva você ao campo de mensagem.\n\n"
             "Na primeira inicialização, a ativação por voz pode baixar um modelo. Você precisa de internet nessa etapa."
             " Ajuste microfone, voz e ativação em Configurações."),
            ("03 / Sua primeira missão", "Tudo pronto para explorar.",
             "Experimente depois de entrar:\n\n  • Que horas são?\n  • Abra a calculadora.\n  • Como está o tempo?\n\n"
             "Com sua chave Gemini, você também pode pedir explicações e conversar sobre ideias.\n\n"
             "Minha conta permite editar nome, cidade e chaves. O botão Tutorial abre este guia novamente.\n\n"
             "Ao fechar a janela principal, continuo ativo na bandeja. Para encerrar e bloquear sua conta, use Sair da conta. "
             "Na próxima abertura, pedirei sua senha.")):
            page = QWizardPage()
            page.setTitle(title)
            page.setSubTitle(subtitle)
            QVBoxLayout(page).addWidget(paragraph(text))
            self.addPage(page)
        self.error = paragraph("")
        layout.addWidget(self.error)

    def validateCurrentPage(self):
        if self.currentId() == 0:
            try:
                self.profile.values(self.session)
            except AccountError as exc:
                self.error.setText(str(exc))
                return False
        return True

    def accept(self):
        try:
            data = self.profile.values(self.session)
            data["tutorial_done"] = True
            self.session.save(data)
        except (AccountError, OSError):
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Não foi possível salvar", "Confira os campos e a permissão de gravação. Seus dados anteriores foram preservados.")
            return
        super().accept()


class AccountDialog(QDialog):
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("APOLO · Minha conta")
        self.resize(960, 700)
        shell = QHBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(IdentityPanel("SEU PERFIL, SUA PRESENÇA"))
        content = QWidget()
        shell.addWidget(content, 1)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 28, 28, 24)
        layout.setSpacing(16)
        brand = paragraph("02  /  MINHA CONTA")
        brand.setObjectName("eyebrow")
        layout.addWidget(brand)
        layout.addWidget(paragraph(f"Sua conta local: {session.username}"))
        self.profile = ProfileForm(session)
        layout.addWidget(self.profile)
        layout.addWidget(paragraph("Gemini usa a nova chave no próximo pedido. Para atualizar as conexões de música, feche o Apolo usando Sair da conta e abra novamente."))
        self.error = paragraph("")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Salvar alterações")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self):
        try:
            self.session.save(self.profile.values(self.session))
        except (AccountError, OSError) as exc:
            self.error.setText(str(exc) if isinstance(exc, AccountError) else "Não foi possível salvar. Verifique a permissão de gravação.")
            return
        self.accept()
