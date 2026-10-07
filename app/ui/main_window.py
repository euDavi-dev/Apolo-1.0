from __future__ import annotations

import psutil
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QKeySequence, QLinearGradient, QPainter, QPen, QRadialGradient, QShortcut
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QPushButton, QScrollArea, QSplitter, QSystemTrayIcon, QVBoxLayout, QWidget, QGridLayout)

from datetime import datetime

from app import APP_TITLE

from app.core.assistant import Assistant
from app.core.config import Settings
from app.core.state import State
from app.ui.boot_overlay import BootOverlay
from app.ui.chat_view import ChatView
from app.ui.core_widget import CoreWidget
from app.ui.icons import make_icon
from app.ui.settings_dialog import SettingsDialog
from app.ui.account_dialog import AccountDialog, TutorialWizard
from app.ui.productivity_dialog import ProductivityDialog
from app.ui.theme import ACCENT, LEVEL_COLORS, MUTED, display_font
from app.utils.textutils import MONTHS_SHORT, WEEKDAYS_SHORT
from app.utils.winfocus import force_foreground


def card() -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(18, 14, 18, 14)
    lay.setSpacing(4)
    return f, lay


class StatusRow(QWidget):
    def __init__(self, name: str):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        self.dot = QLabel("●")
        self.name = QLabel(name)
        self.value = QLabel("—")
        self.name.setObjectName("muted")
        self.name.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        self.value.setMinimumWidth(64)
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.dot)
        lay.addWidget(self.name, 1)
        lay.addWidget(self.value)
        self.set("—", "off")

    def set(self, text: str, level: str) -> None:
        color = LEVEL_COLORS.get(level, MUTED)
        self.dot.setStyleSheet(f"color: {color}; font-size: 10px;")
        self.value.setText(text)
        self.value.setStyleSheet(f"color: {color if level != 'off' else MUTED}; font-size: 12px;")


class TitleBar(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.setObjectName("navigationRail")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(84)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 14, 10, 14)
        lay.setSpacing(8)
        mark = QLabel("a.")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setStyleSheet("color:#ff302e; font-family:'Arial'; font-size:44px; font-weight:700;")
        lay.addWidget(mark)
        lay.addSpacing(8)
        for text, tip, slot in (("◎\nConta", "Perfil e chaves de API", win.open_account),
                                ("▦\nAgenda", "Tarefas, temporizadores e lembretes", win.open_productivity),
                                ("↗\nWhatsApp", "Abrir conversas e preparar mensagens", win.open_whatsapp),
                                ("?\nGuia", "Rever os primeiros passos", win.open_tutorial),
                                ("↗\nSair", "Encerrar o Apolo e exigir senha na próxima abertura", win.quit_app),
                                ("⚙\nAjustes", "Personalizar voz e ativação", win.open_settings),
                                ("−\nOcultar", "Continuar ativo na bandeja", win.hide)):
            button = QPushButton(text)
            button.setObjectName("railAction")
            button.setFixedHeight(56)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            lay.addWidget(button)
        lay.addStretch()
        vertical = QLabel("APOLO")
        vertical.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vertical.setStyleSheet("color:#66585b; font-size:10px;")
        lay.addWidget(vertical)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.win.windowHandle():
            self.win.windowHandle().startSystemMove()


class MainWindow(QMainWindow):
    def __init__(self, assistant: Assistant, cfg: Settings, session=None):
        super().__init__()
        self.a = assistant
        self.cfg = cfg
        self.session = session
        self.state = State.IDLE
        self.tray = None
        self._tray_hint_shown = False
        self._productivity_dialog = None
        self._whatsapp_dialog = None
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(make_icon())
        self.resize(1480, 900)
        self.setMinimumSize(900, 620)

        root = QWidget()
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(TitleBar(self))
        outer = QVBoxLayout()
        outer.setContentsMargins(30, 24, 30, 20)
        outer.setSpacing(20)
        shell.addLayout(outer, 1)

        context = QHBoxLayout()
        context.setSpacing(28)
        clock = QVBoxLayout()
        clock.setSpacing(1)
        self.lbl_time = QLabel("--:--")
        self.lbl_time.setObjectName("clock")
        self.lbl_date = QLabel("")
        self.lbl_date.setObjectName("muted")
        self.lbl_date.setStyleSheet("color:#a89799; font-size:11px;")
        clock.addWidget(self.lbl_time)
        clock.addWidget(self.lbl_date)
        context.addLayout(clock)
        context.addStretch()
        brand = QLabel("APOLO 1.0  /  PRESENÇA DIGITAL")
        brand.setObjectName("eyebrow")
        context.addWidget(brand)
        context.addStretch()
        self.wx_temp = QLabel("--°")
        self.wx_temp.setObjectName("temperature")
        context.addWidget(self.wx_temp)
        weather = QVBoxLayout()
        weather.setSpacing(2)
        self.wx_place = QLabel("—")
        self.wx_place.setObjectName("eyebrow")
        self.wx_cond = QLabel("Buscando dados...")
        self.wx_cond.setObjectName("muted")
        self.wx_more = QLabel("")
        self.wx_more.setObjectName("weatherDetail")
        self.wx_more.setWordWrap(True)
        weather.addWidget(self.wx_place)
        weather.addWidget(self.wx_cond)
        weather.addWidget(self.wx_more)
        context.addLayout(weather)
        outer.addLayout(context)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(18)
        center = QWidget()
        center.setMinimumWidth(320)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 14, 0)
        center_layout.setSpacing(12)
        self.core = CoreWidget()
        self.core.setToolTip("Falar com o Apolo · Ctrl+Espaço")
        self.core.clicked.connect(self.a.listen_now)
        center_layout.addWidget(self.core, 1)
        self.greeting = QLabel(self.idle_greeting())
        self.greeting.setTextFormat(Qt.TextFormat.PlainText)
        self.greeting.setObjectName("heroTitle")
        self.greeting.setWordWrap(True)
        center_layout.addWidget(self.greeting)
        hint = QLabel('Diga “Apolo”, toque na presença ou use Ctrl + Espaço.')
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        center_layout.addWidget(hint)
        shortcuts = QGridLayout()
        shortcuts.setVerticalSpacing(0)
        for index, (label, command) in enumerate((("Que horas são?", "Que horas são?"),
                               ("Como está o tempo?", "Como está o tempo?"))):
            button = QPushButton(label)
            button.setObjectName("quickAction")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda checked=False, text=command: self.a.submit_text(text))
            shortcuts.addWidget(button, 0, index)
        agenda = QPushButton("Minhas tarefas")
        agenda.setObjectName("quickAction")
        agenda.setCursor(Qt.CursorShape.PointingHandCursor)
        agenda.clicked.connect(self.open_productivity)
        shortcuts.addWidget(agenda, 1, 0)
        shortcuts.setColumnStretch(2, 1)
        center_layout.addLayout(shortcuts)
        self.splitter.addWidget(center)
        conversation = QFrame()
        conversation.setObjectName("conversation")
        conversation.setMinimumWidth(290)
        conversation.setLayout(self._build_right())
        self.splitter.addWidget(conversation)
        self.splitter.setStretchFactor(0, 7)
        self.splitter.setStretchFactor(1, 4)
        self.splitter.setSizes([800, 440])
        outer.addWidget(self.splitter, 1)

        self.sidebar = QWidget()
        self.sidebar.setObjectName("systemStrip")
        self.sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.sidebar.setLayout(self._build_left())
        outer.addWidget(self.sidebar)
        self._talk_shortcut = QShortcut(QKeySequence("Ctrl+Space"), self)
        self._talk_shortcut.activated.connect(self.a.listen_now)
        self._input_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        self._input_shortcut.activated.connect(self.input.setFocus)

        self.boot = BootOverlay(root)
        self.boot.raise_()

        self._wire()
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)
        psutil.cpu_percent(None)

    # ---- construção ----------------------------------------------------------
    def _build_left(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setContentsMargins(0, 14, 0, 0)
        col.setSpacing(6)
        grid = QGridLayout()
        grid.setHorizontalSpacing(32)
        grid.setVerticalSpacing(0)
        self.rows = {k: StatusRow(n) for k, n in (("ai", "Inteligência"), ("voice", "Voz"), ("mic", "Microfone"),
                                                 ("clap", "Detector de palmas"), ("wake", "Palavra \"Apolo\""), ("music", "Música"),
                                                 ("weather", "Clima"), ("net", "Conexão"))}
        for i, r in enumerate(self.rows.values()):
            grid.addWidget(r, i // 4, i % 4)
        col.addLayout(grid)
        self.lbl_sys = QLabel("")
        self.lbl_sys.setObjectName("muted")
        self.lbl_sys.setStyleSheet("color:#766669; font-size:10px;")
        self.lbl_sys.setAlignment(Qt.AlignmentFlag.AlignRight)
        col.addWidget(self.lbl_sys)
        return col

    def _build_right(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(16)
        col.setContentsMargins(24, 4, 0, 0)
        head = QHBoxLayout()
        t = QLabel("01  /  CONVERSA")
        t.setObjectName("eyebrow")
        clear = QPushButton("Limpar")
        clear.setObjectName("tb")
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        clear.clicked.connect(self._clear)
        head.addWidget(t)
        head.addStretch(1)
        head.addWidget(clear)
        col.addLayout(head)
        self.chat = ChatView()
        col.addWidget(self.chat, 1)
        composer = QFrame()
        composer.setObjectName("composer")
        compose = QVBoxLayout(composer)
        compose.setContentsMargins(0, 16, 0, 0)
        compose.setSpacing(12)
        self.input = QLineEdit()
        self.input.setObjectName("messageInput")
        self.input.setPlaceholderText("O que passa pela sua cabeça?")
        self.input.setMinimumHeight(44)
        self.input.returnPressed.connect(self._send)
        compose.addWidget(self.input)
        actions = QHBoxLayout()
        tip = QLabel("ENTER PARA ENVIAR  /  CTRL + L")
        tip.setStyleSheet("color:#8f7b7d; font-size:9px;")
        actions.addWidget(tip, 1)
        talk = QPushButton("Falar")
        talk.setObjectName("voiceAction")
        talk.setMinimumHeight(42)
        talk.setToolTip("Ctrl+Espaço")
        talk.setCursor(Qt.CursorShape.PointingHandCursor)
        talk.clicked.connect(self.a.listen_now)
        actions.addWidget(talk)
        send = QPushButton("Enviar ↗")
        send.setObjectName("primary")
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setMinimumHeight(42)
        send.clicked.connect(self._send)
        actions.addWidget(send)
        compose.addLayout(actions)
        col.addWidget(composer)
        return col

    def _wire(self) -> None:
        a = self.a
        a.state_changed.connect(self._on_state)
        a.message_added.connect(self.chat.add)
        a.message_updated.connect(self.chat.update_last)
        a.mic_level.connect(self._on_mic)
        a.out_level.connect(self._on_out)
        a.banner.connect(self.core.show_banner)
        a.boot_line.connect(self.boot.add)
        a.boot_done.connect(self._boot_done)
        a.weather_updated.connect(self._on_weather)
        a.status_changed.connect(lambda k, text, lvl: self.rows[k].set(text, lvl) if k in self.rows else None)
        a.show_requested.connect(self.bring_to_front)
        a.standby_hint.connect(self.core.set_idle_hint)
        productivity_alert = getattr(a, "productivity_alert", None)
        if productivity_alert is not None:
            productivity_alert.connect(self._on_productivity_alert)

    # ---- slots ---------------------------------------------------------------
    def _on_state(self, s: str) -> None:
        self.state = State(s)
        self.core.set_state(self.state)
        self.greeting.setText({State.LISTENING: "Pode falar. Estou ouvindo.",
                               State.PROCESSING: "Só um instante…",
                               State.SPEAKING: "Aqui está.",
                               State.ERROR: "Vamos tentar de novo?"}.get(self.state, self.idle_greeting()))
        if self.state in (State.IDLE, State.PROCESSING):
            self.core.set_level(0.0)

    def _on_mic(self, v: float) -> None:
        if self.state == State.LISTENING:
            self.core.set_level(min(1.0, v * 2.5))

    def _on_out(self, v: float) -> None:
        if self.state == State.SPEAKING:
            self.core.set_level(v)

    def _boot_done(self) -> None:
        self.boot.add("SYSTEM READY")
        self.boot.ready()

    def _on_weather(self, d) -> None:
        self.wx_place.setText(d.place.upper())
        self.wx_temp.setText(f"{d.temp}°{d.unit}")
        self.wx_cond.setText(d.condition.capitalize())
        self.wx_more.setText(f"Umidade {d.humidity}%   ·   Vento {d.wind} km/h\n"
                             f"Máx {d.tmax}°  Mín {d.tmin}°   ·   Chuva {d.rain_prob}%")

    def _send(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.a.submit_text(text)

    def _clear(self) -> None:
        self.chat.clear()
        self.a.clear_history()

    def _tick(self) -> None:
        n = datetime.now()
        self.lbl_time.setText(n.strftime("%H:%M"))
        self.lbl_date.setText(f"{WEEKDAYS_SHORT[n.weekday()]}  {n.day:02d} {MONTHS_SHORT[n.month - 1]} {n.year}")
        if n.second % 2 == 0:
            self.lbl_sys.setText(f"CPU {psutil.cpu_percent(None):.0f}%   ·   RAM {psutil.virtual_memory().percent:.0f}%")

    # ---- janela -------------------------------------------------------------
    def resizeEvent(self, e):
        if hasattr(self, "sidebar"):
            self.sidebar.setVisible(self.width() >= 1180)
        super().resizeEvent(e)

    def showEvent(self, e):
        self._tick()
        self._clock.start(500)
        super().showEvent(e)

    def hideEvent(self, e):
        self._clock.stop()
        super().hideEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#0b0b0d"))
        glow = QRadialGradient(w * 0.36, h * 0.46, h * 0.7)
        glow.setColorAt(0, QColor(210, 10, 15, 22))
        glow.setColorAt(0.55, QColor(140, 8, 14, 9))
        glow.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), glow)
        p.setPen(QPen(QColor(255, 245, 235, 12), 1))
        p.drawLine(112, 126, w - 28, 126)
        p.setPen(QPen(QColor(255, 44, 36, 95), 3))
        p.drawLine(84, 0, 84, 96)
        p.end()

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.showMaximized() if self.cfg.start_maximized else self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        force_foreground(int(self.winId()))

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.a, self)
        if dlg.exec():
            if self.session:
                from copy import deepcopy
                data = deepcopy(self.session.data)
                data["city"] = dlg.result_settings().weather_city
                try:
                    self.session.save(data)
                except (OSError, ValueError):
                    from PySide6.QtWidgets import QMessageBox
                    QMessageBox.warning(self, "Não foi possível salvar", "Confira a cidade (2 a 100 caracteres) e a permissão de gravação.")
                    dlg.deleteLater()
                    return
            self.a.apply_settings(dlg.result_settings())
            if self.tray:
                self.tray.sync()
        dlg.deleteLater()

    def open_productivity(self) -> None:
        if getattr(self.a, "productivity", None) is None:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Agenda", "A agenda está disponível na execução completa do Apolo.")
            return
        if self._productivity_dialog is None:
            self._productivity_dialog = ProductivityDialog(self.a, self)
        self._productivity_dialog.show()
        self._productivity_dialog.raise_()
        self._productivity_dialog.activateWindow()

    def open_whatsapp(self) -> None:
        from app.ui.whatsapp_dialog import WhatsAppDialog
        if getattr(self.a, "whatsapp", None) is None:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "WhatsApp", "O WhatsApp está disponível na execução completa do Apolo.")
            return
        if self._whatsapp_dialog is None:
            self._whatsapp_dialog = WhatsAppDialog(self.a, self)
        self._whatsapp_dialog.show()
        self._whatsapp_dialog.raise_()
        self._whatsapp_dialog.activateWindow()

    def _on_productivity_alert(self, item) -> None:
        title = "Temporizador encerrado" if item["kind"] == "timer" else "Lembrete do Apolo"
        self.core.show_banner(f"{title}: {item['title']}", 12000)
        if self.tray and self.tray.isVisible() and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.showMessage(title, item["title"], QSystemTrayIcon.MessageIcon.Information, 10000)
        else:
            self.bring_to_front()
        acknowledge = getattr(self.a, "acknowledge_productivity_alert", None)
        if acknowledge is not None:
            acknowledge(item["id"])

    def idle_greeting(self) -> str:
        return (f"Olá, {self.session.data['name']}! O que vamos fazer hoje?"
                if self.session else "O que vamos fazer hoje?")

    def _profile_saved(self) -> None:
        from dataclasses import replace
        self.a.apply_settings(replace(self.cfg, weather_city=self.session.data["city"]))
        self._on_state(self.state.value)

    def open_account(self) -> None:
        if self.session:
            dlg = AccountDialog(self.session, self)
            if dlg.exec():
                self._profile_saved()
            dlg.deleteLater()

    def open_tutorial(self) -> None:
        if self.session:
            dlg = TutorialWizard(self.session, self)
            if dlg.exec():
                self._profile_saved()
            dlg.deleteLater()

    def closeEvent(self, e):  # fechar = ir para a bandeja; o APOLO segue ouvindo as palmas
        e.ignore()
        self.hide()
        if self.tray and not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.showMessage("APOLO", "Continuo ativo na bandeja. Diga \"Apolo\" ou bata duas palmas para me chamar.",
                                  QSystemTrayIcon.MessageIcon.Information, 4000)

    def quit_app(self) -> None:
        self.a.shutdown()
        QApplication.quit()
