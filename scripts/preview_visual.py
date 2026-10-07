"""Prévia isolada: desenha a interface sem inicializar microfone ou serviços.

Execute: python scripts/preview_visual.py [--capture PASTA]
Os dados de demonstração existem somente nesta prévia.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtWidgets import QApplication
from app.core.config import Settings
from app.core.state import State
from app.ui.main_window import MainWindow
from app.ui.theme import STYLE
from app.services.productivity_service import ProductivityService
from app.services.whatsapp_service import WhatsAppService


class PreviewAssistant(QObject):
    state_changed = Signal(str)
    message_added = Signal(str, str)
    message_updated = Signal(str)
    mic_level = Signal(float)
    out_level = Signal(float)
    banner = Signal(str)
    boot_line = Signal(str)
    boot_done = Signal()
    weather_updated = Signal(object)
    status_changed = Signal(str, str, str)
    show_requested = Signal()
    standby_hint = Signal(str)
    clap_event = Signal(str)
    calibration_progress = Signal(str)
    calibration_done = Signal(float)
    activation_progress = Signal(str)
    voice_test_progress = Signal(str)
    productivity_changed = Signal(object)
    productivity_alert = Signal(object)

    def __init__(self):
        super().__init__()
        self.cfg = Settings()
        self.actions = []
        self._temporary_data = tempfile.TemporaryDirectory(prefix="apolo-preview-")
        self.productivity = ProductivityService(Path(self._temporary_data.name) / "agenda.sqlite3")
        self.whatsapp = WhatsAppService(opener=lambda url: self.actions.append(("whatsapp", url)) or True)

    def listen_now(self):
        self.actions.append(('listen',))
        self.state_changed.emit(State.LISTENING.value)

    def submit_text(self, text):
        self.actions.append(('submit', text))
        self.message_added.emit('you', text)

    def clear_history(self):
        self.actions.append(('clear',))

    def shutdown(self):
        self.productivity.close()
        self._temporary_data.cleanup()

    def refresh_productivity(self):
        self.productivity_changed.emit(self.productivity.list_items(include_completed=True))

    def calibrate_claps(self):
        pass

    def regenerate_activation_audio(self):
        pass

    def start_voice_test(self):
        return False

    def stop_voice_test(self):
        pass


def create_preview(app):
    a = PreviewAssistant()
    win = MainWindow(a, a.cfg)
    win.boot.hide()
    win.show()
    a.weather_updated.emit(SimpleNamespace(place='São Paulo', temp=24, unit='C',
        condition='céu limpo', humidity=62, wind=12, tmax=27, tmin=18, rain_prob=8))
    for key, text in [('ai', 'Disponível'), ('voice', 'Pronta'), ('mic', 'Ativo'),
                      ('clap', 'Ativo'), ('wake', 'Ativa'), ('music', 'Disponível'),
                      ('weather', 'Atualizado'), ('net', 'Online')]:
        a.status_changed.emit(key, text, 'ok')
    a.standby_hint.emit('DIGA “APOLO” OU BATA DUAS PALMAS')
    return win, a


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', type=Path)
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setStyleSheet(STYLE)
    win, assistant = create_preview(app)
    app.aboutToQuit.connect(assistant.shutdown)
    if args.capture:
        args.capture.mkdir(parents=True, exist_ok=True)
        def capture():
            win.core._timer.stop()
            win.grab().save(str(args.capture / 'apolo.png'))
            app.quit()
        QTimer.singleShot(900, capture)
    sys.exit(app.exec())
