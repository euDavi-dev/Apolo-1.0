from __future__ import annotations

from dataclasses import replace
from PySide6.QtCore import QTimer

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QLabel, QLineEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout,
                               QWidget)

from app.core.config import Settings
from app.services.audio_engine import list_input_devices, list_output_devices

VOICES = ["pt-BR-AntonioNeural", "pt-BR-FranciscaNeural", "pt-BR-ThalitaMultilingualNeural",
          "pt-PT-DuarteNeural", "en-US-AndrewNeural", "en-GB-RyanNeural"]
KOKORO_VOICES = ["pm_alex", "pm_santa", "bm_george", "bm_lewis", "bm_fable", "bm_daniel"]
MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.7-flash"]
CLAP_EVENTS = {"clap": "1ª palma detectada", "double_clap": "DUAS PALMAS — APOLO ativado",
               "rejected_long": "Som longo demais (não é palma)", "rejected_echo": "Eco ignorado"}


def _combo(items, current, editable=False) -> QComboBox:
    cb = QComboBox()
    cb.setEditable(editable)
    for label, data in items:
        cb.addItem(label, data)
    idx = cb.findData(current)
    if idx >= 0:
        cb.setCurrentIndex(idx)
    elif editable:
        cb.setCurrentText(str(current))
    return cb


class SettingsDialog(QDialog):
    def __init__(self, assistant, parent=None):
        super().__init__(parent)
        self.a = assistant
        self.s: Settings = replace(assistant.cfg)
        self.setWindowTitle("Configurações do APOLO")
        self.setMinimumWidth(760)
        self.resize(880, 680)
        s = self.s
        tabs = QTabWidget()

        # Voz
        self.in_dev = _combo([("Padrão do sistema", None)] + [(n, i) for i, n in list_input_devices()], s.input_device)
        self.out_dev = _combo([("Padrão do sistema", None)] + [(n, i) for i, n in list_output_devices()], s.output_device)
        self.tts_engine = _combo([("Neural (edge-tts, online)", "edge"), ("Local (Kokoro, offline)", "kokoro"),
                                 ("Voz do Windows (offline)", "sapi")], s.tts_engine)
        self.voice = _combo([(v, v) for v in VOICES], s.tts_voice, editable=True)
        self.rate = QSpinBox()
        self.rate.setRange(-50, 50)
        self.rate.setSuffix(" %")
        self.rate.setValue(s.tts_rate)
        self.stt = _combo([("Google (gratuito, online)", "google"), ("Gemini (áudio)", "gemini"),
                           ("Local (faster-whisper, offline)", "whisper")], s.stt_engine)
        self.wmodel = _combo([(m, m) for m in ("tiny", "base", "small", "medium", "large-v3-turbo")],
                             s.whisper_model, editable=True)
        self.wmodel.setToolTip("Só para o motor local. Maior = mais preciso e mais lento (rode test_stt_bench).")
        self.pitch = QSpinBox()
        self.pitch.setRange(-30, 30)
        self.pitch.setSuffix(" Hz")
        self.pitch.setValue(s.tts_pitch_hz)
        self.pitch.setToolTip("Voz neural: negativo = mais grave.")
        self.bass = QDoubleSpinBox()
        self.bass.setRange(0.0, 8.0)
        self.bass.setSingleStep(0.5)
        self.bass.setSuffix(" dB")
        self.bass.setValue(s.tts_bass_db)
        self.kvoice = _combo([(v, v) for v in KOKORO_VOICES], s.kokoro_voice, editable=True)
        self.endsil = QSpinBox()
        self.endsil.setRange(300, 1500)
        self.endsil.setSingleStep(50)
        self.endsil.setSuffix(" ms")
        self.endsil.setValue(s.vad_end_silence_ms)
        self.noise_floor = QDoubleSpinBox()
        self.noise_floor.setDecimals(3)
        self.noise_floor.setRange(0.0, 0.03)
        self.noise_floor.setSingleStep(0.001)
        self.noise_floor.setValue(s.vad_min_rms)
        self.noise_floor.setToolTip("Padrão: 0,003. Reduza se sua voz baixa não for ouvida; 0 desativa. Atua na ativação e no comando.")
        self.endsil.setToolTip("Silêncio que confirma o fim da sua fala. Menor = responde mais rápido, "
                               "mas pode cortar pausas longas.")
        tabs.addTab(self._form([("Dispositivo de entrada", self.in_dev), ("Dispositivo de saída", self.out_dev),
                                ("Motor de voz", self.tts_engine), ("Voz", self.voice),
                                ("Voz local (Kokoro)", self.kvoice),
                                ("Velocidade da fala", self.rate), ("Tom (voz neural)", self.pitch),
                                ("Graves", self.bass),
                                ("Reconhecimento de fala", self.stt), ("Modelo local (whisper)", self.wmodel),
                                ("Silêncio para fim da fala", self.endsil), ("Filtro de ruído baixo", self.noise_floor)]), "Voz")

        # Palmas
        self.clap_on = QCheckBox("Ativar detecção de duas palmas")
        self.clap_on.setChecked(s.clap_enabled)
        self.sens = QDoubleSpinBox()
        self.sens.setRange(0.05, 0.95)
        self.sens.setSingleStep(0.01)
        self.sens.setDecimals(2)
        self.sens.setValue(s.clap_sensitivity)
        self.sens.setToolTip("Pico mínimo de áudio (0 a 1). Menor = mais sensível.")
        self.gap_min = QSpinBox()
        self.gap_min.setRange(50, 500)
        self.gap_min.setSuffix(" ms")
        self.gap_min.setValue(s.clap_min_gap_ms)
        self.gap_max = QSpinBox()
        self.gap_max.setRange(300, 3000)
        self.gap_max.setSuffix(" ms")
        self.gap_max.setValue(s.clap_max_gap_ms)
        self.cool = QDoubleSpinBox()
        self.cool.setRange(0.5, 10)
        self.cool.setSingleStep(0.5)
        self.cool.setSuffix(" s")
        self.cool.setValue(s.clap_cooldown_s)
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.ev_label = QLabel("Bata palmas para testar.")
        self.ev_label.setObjectName("muted")
        self.cal_label = QLabel("")
        self.cal_label.setObjectName("muted")
        cal = QPushButton("Calibrar microfone")
        cal.clicked.connect(self.a.calibrate_claps)
        tabs.addTab(self._form([("", self.clap_on), ("Sensibilidade (pico mínimo)", self.sens),
                                ("Intervalo mínimo entre palmas", self.gap_min),
                                ("Intervalo máximo entre palmas", self.gap_max), ("Cooldown", self.cool),
                                ("Nível do microfone", self.meter), ("Último evento", self.ev_label),
                                ("", cal), ("", self.cal_label)]), "Palmas")

        # Ativação por voz
        self.wake_on = QCheckBox("Ativar pela palavra \"Apolo\" (reconhecimento local, sem internet)")
        self.wake_on.setChecked(s.wake_word_enabled)
        self.wake_engine = _combo([("Contínuo + apoio em português (recomendado)", "hybrid"),
                                   ("Somente reconhecimento em português", "whisper")], s.wake_engine)
        self.keyword_threshold = QDoubleSpinBox()
        self.keyword_threshold.setRange(0.08, 0.75)
        self.keyword_threshold.setSingleStep(0.05)
        self.keyword_threshold.setValue(s.wake_keyword_threshold)
        self.keyword_threshold.setToolTip("Limiar do detector contínuo. Menor aceita mais chamadas; maior exige uma pronúncia mais nítida.")
        self.wake_model = _combo([(m, m) for m in ("tiny", "base", "small")], s.wake_model, editable=True)
        self.wake_model.setToolTip("tiny = mais rápido (menos preciso) · base = equilíbrio · small = mais preciso e mais pesado. "
                                   "Na 1ª vez o modelo é baixado (precisa de internet).")
        self.wake_end = QSpinBox()
        self.wake_end.setRange(250, 1000)
        self.wake_end.setSingleStep(50)
        self.wake_end.setSuffix(" ms")
        self.wake_end.setValue(s.wake_end_silence_ms)
        self.wake_end.setToolTip("Silêncio que encerra a frase de ativação. Menor = ativa mais rápido, mas pode separar "
                                 "\"Apolo\" do comando dito logo depois.")
        self.act_cool = QDoubleSpinBox()
        self.act_cool.setRange(0.5, 10)
        self.act_cool.setSingleStep(0.5)
        self.act_cool.setSuffix(" s")
        self.act_cool.setValue(s.activation_cooldown_s)
        self.act_cool.setToolTip("Bloqueia detecções duplicadas e palmas repetidas. Após uma resposta, a voz é rearmada em 350 ms, com proteção contra eco.")
        self.wake_low = QCheckBox("Modo baixa latência (modelo aquecido, transcrição especulativa)")
        self.wake_low.setChecked(s.wake_low_latency)
        self.feedback = _combo([("Sinal curto (mais rápido)", "tone"), ("Falado no botão/palmas; sinal na voz", "voice"),
                                ("Somente indicação visual", "silent")], s.activation_feedback)
        self.prefix = QDoubleSpinBox()
        self.prefix.setRange(0.0, 3.0)
        self.prefix.setSingleStep(0.2)
        self.prefix.setSuffix(" s")
        self.prefix.setValue(s.wake_prefix_s)
        self.prefix.setToolTip("Analisa o início da fala sem esperar o comando terminar. 0 desativa. Funciona no modo baixa latência, sem música tocando.")
        self.wake_conf = QDoubleSpinBox()
        self.wake_conf.setRange(0.3, 0.95)
        self.wake_conf.setSingleStep(0.05)
        self.wake_conf.setValue(s.wake_min_confidence)
        self.wake_conf.setToolTip("Confiança mínima para aceitar \"Apolo\". Suba (0,70-0,75) se ativar sem querer; desça (0,50) se ele te ignorar.")
        self.gen_label = QLabel("")
        self.gen_label.setObjectName("muted")
        self.gen_label.setWordWrap(True)
        regen = QPushButton("Regerar respostas de ativação")
        regen.setToolTip("Gera de novo os 10 áudios curtos (\"Estou aqui.\" etc.) com a voz atual.")
        regen.clicked.connect(self.a.regenerate_activation_audio)
        fast = QPushButton("Aplicar reconhecimento recomendado")
        fast.setToolTip("Ativação híbrida, apoio com modelo base e sinal curto. Salve em OK para aplicar.")
        fast.clicked.connect(self._fast_voice)
        self.voice_test_label = QLabel("Confira aqui se sua chamada está sendo reconhecida.")
        self.voice_test_label.setWordWrap(True)
        self.voice_test_label.setObjectName("muted")
        self.voice_test_button = QPushButton("Testar chamada por 12 segundos")
        self.voice_test_button.clicked.connect(self._test_voice)
        self.voice_test_timer = QTimer(self)
        self.voice_test_timer.setSingleShot(True)
        self.voice_test_timer.timeout.connect(self._finish_voice_test)
        self.a.voice_test_progress.connect(self.voice_test_label.setText)
        tabs.addTab(self._form([("", self.wake_on), ("Sistema de ativação", self.wake_engine),
                                ("", fast), ("", self.voice_test_button), ("", self.voice_test_label),
                                ("Confirmação da ativação", self.feedback), ("Modelo de reconhecimento", self.wake_model),
                                ("", self.wake_low), ("Confiança mínima", self.wake_conf),
                                ("Limiar da palavra-chave", self.keyword_threshold),
                                ("Antecipar reconhecimento após", self.prefix),
                                ("Silêncio para fim da frase", self.wake_end), ("Cooldown entre ativações", self.act_cool),
                                ("", regen), ("", self.gen_label)]), "Ativação")

        # Música
        self.music_on = QCheckBox("Pedir músicas por voz (\"Apolo, toque Believer\")")
        self.music_on.setChecked(s.music_enabled)
        self.music_provider = _combo([("Automático (Spotify > YouTube > pasta local)", "auto"), ("Spotify (Premium)", "spotify"),
                                      ("YouTube (chave de API)", "youtube"), ("Pasta local", "local")], s.music_provider)
        self.music_dir = QLineEdit(s.music_local_dir)
        self.music_dir.setPlaceholderText("vazio = pasta Música do Windows")
        self.music_step = QSpinBox()
        self.music_step.setRange(2, 30)
        self.music_step.setSuffix(" pontos")
        self.music_step.setValue(s.music_volume_step)
        info = QLabel("Edite as chaves em Minha conta. Spotify: rode "
                      "python scripts/music_setup.py spotify uma vez. Detalhes no README.")
        info.setObjectName("muted")
        info.setWordWrap(True)
        tabs.addTab(self._form([("", self.music_on), ("Serviço", self.music_provider), ("Pasta de músicas", self.music_dir),
                                ("Passo do volume", self.music_step), ("", info)]), "Música")

        # IA
        self.model = _combo([(m, m) for m in MODELS], s.gemini_model, editable=True)
        self.temp = QDoubleSpinBox()
        self.temp.setRange(0.0, 2.0)
        self.temp.setSingleStep(0.1)
        self.temp.setValue(s.gemini_temperature)
        self.tokens = QSpinBox()
        self.tokens.setRange(50, 2000)
        self.tokens.setSingleStep(50)
        self.tokens.setValue(s.gemini_max_tokens)
        self.humor = QSpinBox()
        self.humor.setRange(0, 100)
        self.humor.setSuffix(" %")
        self.humor.setValue(int(round(s.persona_humor * 100)))
        self.humor.setToolTip("Frequência máxima de ironias secas nas respostas (0 = nunca).")
        tabs.addTab(self._form([("Modelo Gemini", self.model), ("Temperatura", self.temp),
                                ("Limite de resposta (tokens)", self.tokens),
                                ("Humor seco", self.humor)]), "IA")

        # Clima
        self.city = QLineEdit(s.weather_city)
        self.country = QLineEdit(s.weather_country)
        self.country.setMaxLength(2)
        self.unit = _combo([("Celsius", "celsius"), ("Fahrenheit", "fahrenheit")], s.weather_unit)
        tabs.addTab(self._form([("Cidade padrão", self.city), ("País (código, ex.: BR)", self.country),
                                ("Unidade", self.unit)]), "Clima")

        # Sistema
        self.auto = QCheckBox("Iniciar com o Windows")
        self.auto.setChecked(s.start_with_windows)
        self.minimized = QCheckBox("Abrir minimizado na bandeja")
        self.minimized.setChecked(s.start_minimized)
        self.maximized = QCheckBox("Abrir com a janela maximizada")
        self.maximized.setChecked(s.start_maximized)
        tabs.addTab(self._form([("", self.auto), ("", self.minimized), ("", self.maximized)]), "Sistema")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 20)
        lay.setSpacing(16)
        eyebrow = QLabel("03  /  AJUSTE SUA PRESENÇA")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("Voz, personalidade & preferências.")
        title.setObjectName("heroTitle")
        lay.addWidget(eyebrow)
        lay.addWidget(title)
        lay.addWidget(tabs)
        lay.addWidget(buttons)

        a = self.a
        a.mic_level.connect(self._on_level)
        a.clap_event.connect(self._on_clap)
        a.calibration_progress.connect(self.cal_label.setText)
        a.calibration_done.connect(self.sens.setValue)
        a.activation_progress.connect(self.gen_label.setText)

    def _fast_voice(self) -> None:
        self.wake_model.setCurrentText("base")
        self.wake_engine.setCurrentIndex(self.wake_engine.findData("hybrid"))
        self.keyword_threshold.setValue(0.15)
        self.wake_low.setChecked(True)
        self.wake_conf.setValue(0.50)
        self.wake_end.setValue(300)
        self.prefix.setValue(0.7)
        self.feedback.setCurrentIndex(self.feedback.findData("tone"))
        self.endsil.setValue(500)
        self.s.wake_soft_silence_ms = 140
        self.s.vad_soft_silence_ms = 240
        self.s.stt_speculative = True

    @staticmethod
    def _form(rows) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(18, 18, 18, 18)
        f.setVerticalSpacing(12)
        for label, widget in rows:
            if label:
                f.addRow(label, widget)
            else:
                f.addRow(widget)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        return scroll

    def _on_level(self, v: float) -> None:
        self.meter.setValue(int(min(1.0, v) * 100))

    def _on_clap(self, ev: str) -> None:
        self.ev_label.setText(CLAP_EVENTS.get(ev, ev))

    def done(self, code: int) -> None:
        self.voice_test_timer.stop()
        self.a.stop_voice_test()
        for sig, slot in ((self.a.mic_level, self._on_level), (self.a.clap_event, self._on_clap),
                          (self.a.calibration_progress, self.cal_label.setText),
                          (self.a.calibration_done, self.sens.setValue),
                          (self.a.activation_progress, self.gen_label.setText),
                          (self.a.voice_test_progress, self.voice_test_label.setText)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        super().done(code)

    def _test_voice(self) -> None:
        if self.a.start_voice_test():
            self.voice_test_button.setEnabled(False)
            self.voice_test_timer.start(12000)

    def _finish_voice_test(self) -> None:
        self.a.stop_voice_test()
        self.voice_test_button.setEnabled(True)
        self.voice_test_label.setText(self.voice_test_label.text() + "\nTeste encerrado. Você pode repetir ou salvar novos ajustes.")

    def result_settings(self) -> Settings:
        s = self.s
        s.input_device, s.output_device = self.in_dev.currentData(), self.out_dev.currentData()
        s.tts_engine, s.tts_voice = self.tts_engine.currentData(), self.voice.currentText().strip() or s.tts_voice
        s.tts_rate, s.stt_engine = self.rate.value(), self.stt.currentData()
        s.whisper_model = self.wmodel.currentText().strip() or s.whisper_model
        s.tts_pitch_hz, s.tts_bass_db = self.pitch.value(), round(self.bass.value(), 1)
        s.kokoro_voice = self.kvoice.currentText().strip() or s.kokoro_voice
        s.vad_end_silence_ms = self.endsil.value()
        s.vad_min_rms = self.noise_floor.value()
        s.vad_soft_silence_ms = min(s.vad_soft_silence_ms, max(100, s.vad_end_silence_ms - 100))
        s.persona_humor = round(self.humor.value() / 100.0, 2)
        s.wake_word_enabled = self.wake_on.isChecked()
        s.wake_low_latency = self.wake_low.isChecked()
        s.activation_feedback = self.feedback.currentData()
        s.wake_prefix_s = self.prefix.value()
        s.wake_min_confidence = round(self.wake_conf.value(), 2)
        s.wake_engine = self.wake_engine.currentData()
        s.wake_keyword_threshold = self.keyword_threshold.value()
        s.music_enabled = self.music_on.isChecked()
        s.music_provider = self.music_provider.currentData() or "auto"
        s.music_local_dir = self.music_dir.text().strip()
        s.music_volume_step = self.music_step.value()
        s.wake_model = self.wake_model.currentText().strip() or s.wake_model
        s.wake_end_silence_ms = self.wake_end.value()
        s.wake_soft_silence_ms = min(s.wake_soft_silence_ms, max(100, s.wake_end_silence_ms - 150)) if s.wake_soft_silence_ms else 0
        s.activation_cooldown_s = self.act_cool.value()
        s.clap_enabled, s.clap_sensitivity = self.clap_on.isChecked(), round(self.sens.value(), 2)
        s.clap_min_gap_ms, s.clap_max_gap_ms = self.gap_min.value(), max(self.gap_max.value(), self.gap_min.value() + 100)
        s.clap_cooldown_s = self.cool.value()
        s.gemini_model = self.model.currentText().strip() or s.gemini_model
        s.gemini_temperature, s.gemini_max_tokens = round(self.temp.value(), 2), self.tokens.value()
        s.weather_city, s.weather_country = self.city.text().strip() or s.weather_city, self.country.text().strip().upper()
        s.weather_unit = self.unit.currentData()
        s.start_with_windows, s.start_minimized = self.auto.isChecked(), self.minimized.isChecked()
        s.start_maximized = self.maximized.isChecked()
        return s
