"""Verificação do pacote, sem abrir microfone, gravar conta real ou chamar APIs."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback


def _bundled_keyword_model_check():
    """Valida o modelo local opcional sem passar pelo fluxo de download do app."""
    from app.core.config import ROOT, Settings
    from app.services.keyword_spotter import FILES, model_valid
    model = ROOT / "models" / "wake" / "keyword"
    if not model_valid(model):
        if any((model / filename).exists() for filename in FILES.values()):
            reason = "Modelo local incompleto ou com integridade inválida; nenhum download foi realizado."
        else:
            reason = "Pesos, léxico e tokens não incluídos na versão fonte; nenhum download foi realizado."
        return {"status": "skipped", "reason": reason}

    import numpy as np
    import sherpa_onnx
    # Instancia diretamente a biblioteca com caminhos locais verificados. O
    # método load() do app não é usado porque pode baixar um modelo ausente.
    with tempfile.TemporaryDirectory(prefix="apolo-kws-selftest-") as temporary:
        keywords = Path(temporary) / "keywords.txt"
        keywords.write_text("AH0 P AO1 L UW0 @APOLO\n", encoding="utf-8")
        spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(model / FILES["tokens"]), encoder=str(model / FILES["encoder"]),
            decoder=str(model / FILES["decoder"]), joiner=str(model / FILES["joiner"]),
            keywords_file=str(keywords), num_threads=1, keywords_score=2.0,
            max_active_paths=8, keywords_threshold=Settings().wake_keyword_threshold,
            num_trailing_blanks=1,
        )
        stream = spotter.create_stream()
        stream.accept_waveform(16000, np.zeros(16000, dtype=np.float32))
        while spotter.is_ready(stream):
            spotter.decode_stream(stream)
        assert not spotter.keyword_spotter.get_result(stream).keyword.strip()
    return {"status": "passed", "source": "bundled", "input": "silence"}


def main():
    report = Path(sys.argv[sys.argv.index("--self-test") + 1]).resolve()
    result = {"frozen": bool(getattr(sys, "frozen", False)), "checks": {}, "errors": {}}
    with tempfile.TemporaryDirectory(prefix="apolo-package-") as temporary:
        os.environ["APPDATA"] = temporary
        os.environ["HF_HOME"] = str(Path(temporary) / "huggingface")
        modules = ["numpy", "sounddevice", "webrtcvad", "speech_recognition", "google.genai",
                   "edge_tts", "miniaudio", "pyttsx3.drivers.sapi5", "requests", "dotenv", "psutil",
                   "cryptography", "faster_whisper", "sherpa_onnx", "mutagen", "kokoro_onnx",
                   "app.core.assistant", "app.ui.main_window", "app.ui.settings_dialog"]
        for name in modules:
            try:
                importlib.import_module(name)
                result["checks"][name] = True
            except Exception:
                result["errors"][name] = traceback.format_exc()

        def check(name, action):
            try:
                action()
                result["checks"][name] = True
            except Exception:
                result["errors"][name] = traceback.format_exc()

        def native_audio():
            import numpy as np
            import webrtcvad
            import ctranslate2
            assert 'int8' in ctranslate2.get_supported_compute_types('cpu')
            from speech_recognition import AudioData
            # Converte silêncio para FLAC usando o executável incluído no pacote.
            data = AudioData(np.zeros(1600, dtype=np.int16).tobytes(), 16000, 2)
            assert data.get_flac_data().startswith(b"fLaC")
            assert not webrtcvad.Vad(2).is_speech(bytes(640), 16000)
            from app.core.config import Settings
            assert Settings().wake_min_confidence == 0.5
        check("native_audio", native_audio)

        # O resultado opcional é explícito e não impede validar uma versão
        # fonte sem pesos. Bibliotecas nativas ausentes continuam em errors.
        try:
            result["checks"]["bundled_keyword_model"] = _bundled_keyword_model_check()
        except Exception:
            result["checks"]["bundled_keyword_model"] = {"status": "failed"}
            result["errors"]["bundled_keyword_model"] = traceback.format_exc()

        def interface():
            from PySide6.QtWidgets import QApplication
            from PySide6.QtGui import QFontDatabase
            from app.core.accounts import AccountStore
            from app.ui.account_dialog import LoginDialog
            from app.ui.icons import make_icon
            from app.ui.theme import STYLE
            application = QApplication.instance() or QApplication([])
            application.setStyleSheet(STYLE)
            assert QFontDatabase.families(), "Fontes da interface indisponíveis"
            dialog = LoginDialog(AccountStore(Path(temporary) / "accounts"))
            dialog.setWindowIcon(make_icon())
            dialog.ensurePolished()
            dialog.adjustSize()
            application.processEvents()
            assert dialog.grab().save(str(report.with_suffix(".png")))
            dialog.close()
        check("login_interface", interface)

        def startup():
            from app.utils.autostart import _command
            command = _command()
            assert "--minimized" in command
            if getattr(sys, "frozen", False):
                assert 'run.py' not in command
                assert sys.executable in command
        check("windows_startup_command", startup)
    result["ok"] = not result["errors"]
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    raise SystemExit(0 if result["ok"] else 1)
