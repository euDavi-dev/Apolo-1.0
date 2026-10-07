from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from app import APP_TITLE, __version__

from app.core.config import Settings
from app.core.accounts import AccountStore, activate
from app.ui.account_dialog import LoginDialog, TutorialWizard
from app.ui.icons import make_icon
from app.ui.theme import STYLE
from app.ui.tray import Tray

log = logging.getLogger("apolo")


def main() -> None:
    sys.excepthook = lambda *exc: log.critical("Exceção não tratada", exc_info=exc)
    threading.excepthook = lambda a: log.critical("Exceção em thread %s", a.thread, exc_info=a.exc_value)

    app = QApplication(sys.argv)
    app.setApplicationName("APOLO")
    app.setApplicationDisplayName(APP_TITLE)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(make_icon())
    app.setQuitOnLastWindowClosed(False)  # segue vivo na bandeja
    app.setStyleSheet(STYLE)

    lock = QLockFile(os.path.join(tempfile.gettempdir(), "apolo-assistant.lock"))
    if not lock.tryLock(100):
        QMessageBox.information(None, "APOLO", "O APOLO já está em execução (veja o ícone na bandeja).")
        return

    from app.core import config
    login = LoginDialog(AccountStore(config.DATA_DIR))
    if not login.exec():
        lock.unlock()
        return
    session = login.session
    login.deleteLater()
    if not session.data.get("tutorial_done"):
        tutorial = TutorialWizard(session)
        if not tutorial.exec():
            lock.unlock()
            return
        tutorial.deleteLater()
    activate(session)
    # Serviços importam caminhos de dados: só carregá-los após selecionar a conta.
    from app.core.assistant import Assistant
    from app.ui.main_window import MainWindow
    from app.utils.logger import setup_logging
    setup_logging()
    cfg = Settings.load()
    cfg.weather_city = session.data["city"]
    assistant = Assistant(cfg)
    win = MainWindow(assistant, cfg, session)
    tray = Tray(win, assistant, cfg)
    win.tray = tray
    tray.show()
    if "--minimized" not in sys.argv and not cfg.start_minimized:
        win.showMaximized() if cfg.start_maximized else win.show()
    assistant.boot()

    code = app.exec()
    assistant.shutdown()
    lock.unlock()
    sys.exit(code)
