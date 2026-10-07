from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from app.ui.icons import make_icon
from app.utils import autostart


class Tray(QSystemTrayIcon):
    def __init__(self, win, assistant, cfg):
        super().__init__(make_icon(), win)
        self.win, self.a, self.cfg = win, assistant, cfg
        self.setToolTip("APOLO — diga \"Apolo\" ou bata duas palmas")
        menu = QMenu()
        menu.addAction("Abrir APOLO", win.bring_to_front)
        menu.addAction("Agenda: tarefas e lembretes", win.open_productivity)
        menu.addAction("WhatsApp...", win.open_whatsapp)
        self.act_clap = QAction("Detectar duas palmas", menu, checkable=True)
        self.act_clap.toggled.connect(self._clap)
        menu.addAction(self.act_clap)
        self.act_wake = QAction("Detectar \"Apolo\" (voz)", menu, checkable=True)
        self.act_wake.toggled.connect(self._wake)
        menu.addAction(self.act_wake)
        self.act_auto = QAction("Iniciar com o Windows", menu, checkable=True)
        self.act_auto.toggled.connect(self._auto)
        menu.addAction(self.act_auto)
        menu.addAction("Configurações...", win.open_settings)
        menu.addSeparator()
        menu.addAction("Sair", win.quit_app)
        self._menu = menu
        self.setContextMenu(menu)
        self.activated.connect(self._activated)
        self.sync()

    def sync(self) -> None:
        for act, val in ((self.act_clap, self.cfg.clap_enabled), (self.act_wake, self.cfg.wake_word_enabled),
                         (self.act_auto, self.cfg.start_with_windows)):
            act.blockSignals(True)
            act.setChecked(val)
            act.blockSignals(False)

    def _clap(self, on: bool) -> None:
        self.a.set_clap_enabled(on)

    def _wake(self, on: bool) -> None:
        self.a.set_wake_enabled(on)

    def _auto(self, on: bool) -> None:
        self.cfg.start_with_windows = on
        self.cfg.save()
        autostart.set_enabled(on)

    def _activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.win.isVisible():
                self.win.hide()
            else:
                self.win.bring_to_front()
