"""Orquestrador do APOLO: liga microfone, palmas, STT, Gemini, TTS, clima, comandos e UI.

Ativação: DUAS fontes independentes (palmas e a palavra "Apolo") alimentam o MESMO ponto,
activate_apolo(source, command). Em standby o microfone entrega cada bloco a _standby_sink, que roda o detector de
palmas e o detector de wake word (VAD + faster-whisper local) em paralelo.

Fases (State): IDLE = STANDBY/escutando "Apolo" e palmas · ACTIVATED = palavra/palmas detectadas + resposta de
ativação · LISTENING = escutando o comando · PROCESSING · SPEAKING · de volta a IDLE.

Regras de concorrência:
- Existe no máximo UMA "sessão" (ouvir -> entender -> responder) por vez (self._lock).
- Durante a sessão os detectores (palmas E voz) ficam desligados — evita que a própria voz do APOLO os dispare;
  ao terminar, são rearmados com cooldown/guarda (o wake word ignora a cauda do áudio do TTS).
- Todo trabalho pesado roda em threads; a UI só recebe sinais Qt."""
from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import fields
from datetime import datetime

from PySide6.QtCore import QObject, Signal

from app.core.config import ACTIVATION_DIR, ACTIVATION_PHRASES, Settings
from app.core.state import PHASES, State
from app.services import intent_service
from app.services.activation_audio import ActivationAudio, ActivationGenError, generate as generate_activation_audio
from app.services.audio_engine import AudioEngine
from app.services.clap_detector import ClapCalibrator, ClapConfig, ClapDetector
from app.services.command_service import CommandService, is_yes
from app.services.music_service import MUSIC_FIXED_PHRASES, MusicService
from app.services.productivity_service import ProductivityError, ProductivityService
from app.services.whatsapp_service import WhatsAppService
from app.services.gemini_service import GeminiError, GeminiService
from app.services.speech_service import SpeechError, SpeechService
from app.services.tts_service import TTSService
from app.services.wake_word import WakeWordDetector, WhisperWakeSTT, match_wake
from app.services.keyword_spotter import StreamingKeywordSpotter
from app.services.weather_service import WeatherError, WeatherService
from app.utils import autostart
from app.utils.metrics import Turn
from app.utils.sounds import play_activation_sound
from app.utils.textutils import clean_for_display, clean_for_tts, pop_sentences, spoken_date, spoken_time

log = logging.getLogger("apolo.assistant")
SPLIT_MIN_CHARS = 90   # respostas locais mais curtas que isso são faladas de uma vez


class Assistant(QObject):
    state_changed = Signal(str)
    message_added = Signal(str, str)        # papel ("you"|"apolo"|"system"), texto
    message_updated = Signal(str)           # atualiza a última bolha (streaming)
    mic_level = Signal(float)
    out_level = Signal(float)
    banner = Signal(str)
    boot_line = Signal(str)
    boot_done = Signal()
    weather_updated = Signal(object)
    status_changed = Signal(str, str, str)  # chave, texto, nível (ok|warn|err|off)
    clap_event = Signal(str)
    calibration_progress = Signal(str)
    calibration_done = Signal(float)
    show_requested = Signal()
    standby_hint = Signal(str)              # texto de ajuda do HUD em standby (ex.: DIGA "APOLO" OU BATA DUAS PALMAS)
    activation_progress = Signal(str)       # progresso da geração das respostas de ativação
    voice_test_progress = Signal(str)
    productivity_changed = Signal(object)
    productivity_alert = Signal(object)

    def __init__(self, cfg: Settings, productivity=None):
        super().__init__()
        self.cfg = cfg
        self.state = State.IDLE
        self.gemini = GeminiService(cfg)
        self.weather = WeatherService(cfg)
        self.commands = CommandService(ack=self.gemini.persona.open_ack)
        self.speech = SpeechService(cfg, self.gemini)
        self.tts = TTSService(cfg, on_level=self.out_level.emit,
                              on_engine=lambda label: self.status_changed.emit("voice", label, "ok"))
        self.audio = AudioEngine(cfg.input_device, on_level=self.mic_level.emit, on_status=self._on_mic_status)
        self.clap_cfg = ClapConfig()
        self._sync_clap_cfg()
        self.detector = ClapDetector(self.clap_cfg)
        # Ativação por voz: STT local (faster-whisper; reaproveita o modelo do STT principal se for o mesmo) + VAD
        self.wake_stt = WhisperWakeSTT(cfg, shared_loader=self.speech._load_whisper)
        # audio_clock: hora em que o microfone ENTREGOU cada bloco (e não a do processamento) => latências honestas
        self.wake = WakeWordDetector(cfg, transcribe=self.wake_stt, on_wake=self._on_wake,
                                     audio_clock=lambda: getattr(self.audio, "block_ts", 0.0) or time.perf_counter())
        self.wake.is_busy = lambda: bool(getattr(self.tts, "busy", False))   # nunca escuta enquanto o TTS fala
        self.music = MusicService(cfg, on_state=lambda text, lvl: self.status_changed.emit("music", text, lvl))
        self.wake.noisy = lambda: self.music.is_playing         # música tocando: VAD mais rígido, letras cantadas não vão ao STT
        self.wake.streaming = StreamingKeywordSpotter(cfg)
        self.wake.on_heard = self._voice_test_heard
        self._voice_test_active = False
        self._wake_info = None                                  # linha do tempo da ativação por voz em andamento
        self.activation = ActivationAudio(ACTIVATION_DIR, ACTIVATION_PHRASES)   # WAVs pré-renderizados, em memória
        self._last_activation = -1e9
        self._wake_warming = False
        self._lock = threading.Lock()
        self._quit = threading.Event()
        self._pending = None          # (comando, argumento, timestamp) aguardando confirmação
        self._mic_ok: bool | None = None
        self._turn = Turn()           # cronometragem do turno atual (ver app/utils/metrics.py)
        self.productivity = productivity if productivity is not None else ProductivityService()
        self.whatsapp = WhatsAppService()
        self._productivity_thread = None
        self._productivity_error_reported = False
        self._productivity_pending = set()
        self._productivity_notifications_lock = threading.Lock()

    # ------------------------------------------------------------------ boot
    def boot(self) -> None:
        if self._productivity_thread is None:
            self._productivity_thread = threading.Thread(
                target=self._productivity_loop, daemon=True, name="productivity")
            self._productivity_thread.start()
        threading.Thread(target=self._boot, daemon=True, name="boot").start()

    def refresh_productivity(self) -> None:
        if self._quit.is_set():
            return
        try:
            items = self.productivity.list_items(include_completed=True)
        except ProductivityError as exc:
            if not self._quit.is_set():
                self.message_added.emit("system", str(exc))
            return
        if not self._quit.is_set():
            self.productivity_changed.emit(items)

    def _poll_productivity(self) -> None:
        if self._quit.is_set():
            return
        try:
            due = self.productivity.poll_due()
        except ProductivityError as exc:
            log.warning("Agenda: %s", exc)
            if not self._productivity_error_reported and not self._quit.is_set():
                self.message_added.emit("system", str(exc))
                self._productivity_error_reported = True
            return
        self._productivity_error_reported = False
        for index, item in enumerate(due):
            if self._quit.is_set():
                self._requeue_productivity(due[index:])
                return
            with self._productivity_notifications_lock:
                self._productivity_pending.add(item["id"])
            title = "Temporizador encerrado" if item["kind"] == "timer" else "Lembrete"
            # Avisos não interrompem a voz nem substituem a bolha em streaming.
            self.message_added.emit("system", f"{title}: {item['title']}")
            if self._quit.is_set():
                self._requeue_productivity(due[index:])
                return
            self.productivity_alert.emit(item)
        if due and not self._quit.is_set():
            self.refresh_productivity()

    def acknowledge_productivity_alert(self, item_id: int) -> None:
        """A interface confirma que processou o aviso enviado pela thread."""
        with self._productivity_notifications_lock:
            self._productivity_pending.discard(item_id)

    def _requeue_productivity(self, items) -> None:
        ids = [item["id"] for item in items]
        if not ids:
            return
        try:
            self.productivity.requeue_due(ids)
        except ProductivityError as exc:
            log.warning("Não foi possível recuperar avisos pendentes: %s", exc)
            return
        with self._productivity_notifications_lock:
            self._productivity_pending.difference_update(ids)

    def _productivity_loop(self) -> None:
        self._poll_productivity()  # recupera alertas vencidos enquanto o app estava fechado
        while not self._quit.wait(1):
            self._poll_productivity()

    def _boot(self) -> None:
        emit = self.boot_line.emit
        emit("SYSTEM INITIALIZING...")
        # Uma conexão lenta com o clima ou a IA não deve bloquear o microfone e a interface.
        def check_ai():
            ai_err = self._check_ai()
            emit("AI CORE ONLINE" if not ai_err else "AI CORE OFFLINE")
            if ai_err:
                self.message_added.emit("system", ai_err)

        def check_weather():
            ok = self._refresh_weather(True)
            emit("WEATHER SYSTEM ONLINE" if ok else "WEATHER SYSTEM OFFLINE")

        threading.Thread(target=check_ai, daemon=True, name="boot-ai").start()
        threading.Thread(target=check_weather, daemon=True, name="boot-weather").start()
        mic_ok = self.audio.start()
        emit("VOICE SYSTEM ONLINE" if mic_ok else "VOICE SYSTEM OFFLINE")
        self.tts.start()
        self.status_changed.emit("voice", self.tts.label(), "ok")
        if self.cfg.stt_engine == "whisper":
            threading.Thread(target=self.speech.warm, daemon=True, name="stt-warm").start()
        n_clips = self.activation.load()           # respostas de ativação para a memória (arquivos locais, ms)
        log.info("[APOLO] %d/%d respostas de ativação carregadas na memória", n_clips, len(self.activation.phrases))
        if self.cfg.activation_feedback == "voice":
            self._ensure_activation_audio()
        if self.cfg.music_enabled:
            self.status_changed.emit("music", "OFF", "off")
            self.music.warm()                      # indexa a pasta local em segundo plano
        if self.cfg.wake_word_enabled:
            emit("WAKE WORD ENGINE LOADING...")
            threading.Thread(target=self._warm_wake, daemon=True, name="wake-warm").start()
        labels = [c.label for c in self.commands.commands if c.label]
        self.tts.prerender(self.gemini.persona.fixed_phrases(labels) + MUSIC_FIXED_PHRASES, busy=self._lock.locked)
        self._resume_listening()
        emit("CLAP DETECTOR ONLINE" if (self.cfg.clap_enabled and mic_ok) else "CLAP DETECTOR STANDBY")
        self.boot_done.emit()
        threading.Thread(target=self._background_loop, daemon=True, name="background").start()

    def _check_ai(self) -> str:
        try:
            self.gemini.check()
        except GeminiError as e:
            self.status_changed.emit("ai", "OFFLINE", "err")
            return e.message
        self.status_changed.emit("ai", "ONLINE", "ok")
        return ""

    def _refresh_weather(self, force: bool = False) -> bool:
        try:
            data = self.weather.fetch(force)
        except WeatherError as e:
            log.warning("Clima: %s", e)
            self.status_changed.emit("weather", "OFFLINE", "err")
            return False
        self.weather_updated.emit(data)
        self.status_changed.emit("weather", "ONLINE", "ok")
        return True

    def _background_loop(self) -> None:
        tick = 0
        while not self._quit.wait(20):
            tick += 1
            try:
                socket.create_connection(("1.1.1.1", 443), timeout=2).close()
                self.status_changed.emit("net", "ONLINE", "ok")
            except OSError:
                self.status_changed.emit("net", "OFFLINE", "err")
            if tick % 45 == 0:  # ~15 min
                self._refresh_weather(True)

    def _on_mic_status(self, ok: bool, msg: str) -> None:
        self.status_changed.emit("mic", "ONLINE" if ok else "OFFLINE", "ok" if ok else "err")
        if ok != self._mic_ok and msg:
            self.message_added.emit("system", msg)
        self._mic_ok = ok

    # ---------------------------------------------- escuta em standby (palmas + "Apolo")
    def _sync_clap_cfg(self) -> None:
        c, s = self.clap_cfg, self.cfg
        c.sensitivity, c.min_gap_ms = s.clap_sensitivity, s.clap_min_gap_ms
        c.max_gap_ms, c.cooldown_s = s.clap_max_gap_ms, s.clap_cooldown_s

    def _standby_sink(self, block) -> None:
        """Recebe cada bloco de 20 ms em standby. Palmas e voz rodam EM PARALELO e ativam o mesmo ponto."""
        if self.cfg.clap_enabled:
            ev = self.detector.process(block)
            if ev:
                self.clap_event.emit(ev)
                if ev == "double_clap":
                    log.info("[APOLO] Clap activation detected")
                    self.activate_apolo("clap")
        self.wake.feed(block)          # só VAD enquanto ninguém fala; o STT roda numa thread à parte

    def _wake_wanted(self) -> bool:
        return bool(self.cfg.wake_word_enabled and (not self.wake_stt.failed or self.wake.streaming.ready))

    def _wake_status(self) -> tuple[str, str]:
        if not self.cfg.wake_word_enabled:
            return "OFF", "off"
        if self.wake_stt.failed and not self.wake.streaming.ready:
            return "UNAVAILABLE", "err"
        return ("ARMED", "ok") if self.wake.ready else ("LOADING", "warn")

    def _standby_hint(self) -> str:
        how = []
        if self._wake_wanted():
            how.append(f'DIGA "{self.cfg.wake_word.upper()}"')
        if self.cfg.clap_enabled:
            how.append("BATA DUAS PALMAS")
        return " OU ".join(how)

    def _resume_listening(self) -> None:
        if self.cfg.clap_enabled or self._wake_wanted():
            if not self.audio.ok:
                self.audio.start()
            self.audio.flush()
            self.detector.reset(cooldown_s=0.5)
            self.wake.reset(guard_s=self.cfg.wake_post_speech_guard_s, rearm=True)   # ignora a cauda do áudio do APOLO
            self.wake.muted = False
            self.audio.set_sink(self._standby_sink)
            self.status_changed.emit("clap", "ARMED" if self.cfg.clap_enabled else "OFF", "ok" if self.cfg.clap_enabled else "off")
            self.status_changed.emit("wake", *self._wake_status())
        else:
            self.audio.set_sink(None)
            self.audio.stop()
            self.status_changed.emit("mic", "STANDBY", "off")
            self.status_changed.emit("clap", "OFF", "off")
            self.status_changed.emit("wake", *self._wake_status())
        self.standby_hint.emit(self._standby_hint())

    def set_clap_enabled(self, enabled: bool) -> None:
        self.cfg.clap_enabled = enabled
        self.cfg.save()
        if not self._lock.locked():
            self._resume_listening()

    def set_wake_enabled(self, enabled: bool) -> None:
        self.cfg.wake_word_enabled = enabled
        self.cfg.save()
        if enabled and not self.wake.ready:
            threading.Thread(target=self._warm_wake, daemon=True, name="wake-warm").start()
        if not self._lock.locked():
            self._resume_listening()

    def _warm_wake(self) -> None:
        """Carrega o modelo do wake word em segundo plano (o boot e as palmas não esperam por isso)."""
        if self._wake_warming:
            return
        self._wake_warming = True
        try:
            self.status_changed.emit("wake", "LOADING", "warn")
            log.info("[APOLO] Wake-word engine initializing (model %s)", self.cfg.wake_model)
            keyword_ok = self.cfg.wake_engine == "hybrid" and self.wake.streaming.load()
            self.wake.ready = bool(keyword_ok)
            if keyword_ok:
                self.status_changed.emit("wake", "CONTINUOUS", "ok")
            whisper_ok = self.wake_stt.load()
            ok = whisper_ok or keyword_ok
            self.wake.ready = ok
            if ok:
                log.info('[APOLO] Wake-word engine initialized')
                log.info('[APOLO] Listening for "%s"', self.cfg.wake_word)
                if keyword_ok:
                    log.info("[WAKE] Detector contínuo pronto; reconhecimento em português como apoio")
                elif self.cfg.wake_engine == "hybrid":
                    self.message_added.emit("system", self.wake.streaming.error)
            else:
                self.message_added.emit("system", self.wake_stt.error)
            self.status_changed.emit("wake", *self._wake_status())
            self.standby_hint.emit(self._standby_hint())
            if not ok and not self._lock.locked():
                self._resume_listening()        # sem wake word e sem palmas: o microfone pode descansar
        finally:
            self._wake_warming = False

    def _on_wake(self, command: str, heard: str, info=None) -> bool:
        if self._voice_test_active:
            self.voice_test_progress.emit(f'Chamada reconhecida: “{heard[:120]}”. O teste não executa pedidos.')
            return True
        return self.activate_apolo("voice", command, info)

    def _voice_test_heard(self, text: str, matched: bool) -> None:
        if self._voice_test_active and not matched:
            self.voice_test_progress.emit(f'Ouvi “{text[:120]}”. Experimente dizer “{self.cfg.wake_word}” no começo.')

    def start_voice_test(self) -> bool:
        if not self.audio.ok:
            self.voice_test_progress.emit("Microfone indisponível. Confira o dispositivo na aba Voz antes de testar.")
            return False
        if self._lock.locked() or not self.cfg.wake_word_enabled or not self.wake.ready:
            self.voice_test_progress.emit("Aguarde o Apolo ficar disponível e salve a ativação por voz ligada para testar.")
            return False
        self._voice_test_active = True
        self.wake.reset()
        self.voice_test_progress.emit(f'Diga “{self.cfg.wake_word}” ou “{self.cfg.wake_word}, que horas são?”. Teste de 12 segundos com os ajustes já salvos.')
        return True

    def stop_voice_test(self) -> None:
        if self._voice_test_active:
            self.wake.reset()  # invalida resultados tardios antes de permitir pedidos reais
            self._voice_test_active = False

    # ----------------------------------------- respostas de ativação pré-renderizadas
    def _ensure_activation_audio(self) -> None:
        sig = self.tts.voice_signature(self.cfg.tts_engine) if self.cfg.tts_engine != "sapi" else None
        if self.activation.needs_generation(sig):
            log.info("[APOLO] Respostas de ativação ausentes/desatualizadas: gerando em segundo plano")
            threading.Thread(target=self._generate_activation_audio, args=(False,), daemon=True,
                             name="activation-gen").start()

    def regenerate_activation_audio(self) -> None:
        """Regera os WAVs com a voz atual (botão em Configurações > Ativação)."""
        threading.Thread(target=self._generate_activation_audio, args=(True,), daemon=True, name="activation-gen").start()

    def _generate_activation_audio(self, manual: bool) -> None:
        try:
            if not manual:
                time.sleep(6)                      # deixa o boot e a 1ª sessão em paz
                while self._lock.locked():
                    time.sleep(2)
            sig = self.tts.voice_signature(self.cfg.tts_engine) if self.cfg.tts_engine != "sapi" else None
            generate_activation_audio(self.tts, self.activation.phrases, self.activation.dir, sig,
                                      progress=self.activation_progress.emit)
            n = self.activation.load()
            self.activation_progress.emit(f"{n} respostas de ativação prontas.")
            if manual:
                self.message_added.emit("system", f"{n} respostas de ativação regeneradas com a voz atual.")
        except ActivationGenError as e:
            log.warning("[APOLO] %s", e)
            self.activation_progress.emit(str(e))
            if manual:
                self.message_added.emit("system", str(e))
        except Exception:
            log.exception("Falha ao gerar as respostas de ativação")
            self.activation_progress.emit("Falha ao gerar as respostas de ativação (veja o log).")

    # --------------------------------------------------------------- sessões
    def activate_apolo(self, source: str = "voice", command: str = "", info=None) -> bool:
        """PONTO ÚNICO de ativação. source: "clap" | "voice" | "ui". `command` (voz): o que veio junto com o nome
        ("Apolo, que horas são?" -> "que horas são?"); se houver, é executado direto, sem resposta de ativação.

        `info` (WakeInfo): linha do tempo da detecção por voz, para as métricas.

        Devolve False se ignorou (cooldown/debounce ou já existe uma sessão). Thread-safe."""
        now = time.monotonic()
        cooldown = min(self.cfg.activation_cooldown_s, self.cfg.wake_rearm_s) if source == "voice" else self.cfg.activation_cooldown_s
        if source != "ui" and now - self._last_activation < cooldown:
            log.info("[APOLO] Activation ignored (cooldown, source=%s)", source)
            return False
        if not self._lock.acquire(blocking=False):
            log.info("[APOLO] Activation ignored (busy, source=%s)", source)
            return False
        self._last_activation = now
        carry = None
        if source == "voice":
            # Troca atômica do microfone: o que chegar daqui em diante fica no backlog do AudioEngine e o que o detector
            # já tinha ouvido depois de "Apolo" vem em `carry`. Se a pessoa já começou o comando, nada se perde.
            self.audio.set_sink(None, keep_backlog=True)
            carry = self.wake.take_carry()
        else:
            self.wake.muted = True             # a partir daqui o microfone não vira mais ativação
            self.audio.set_sink(None)
        if info is not None:
            info.t_activate = time.perf_counter()
        log.info("[APOLO] Activation source: %s", source)
        threading.Thread(target=self._session, args=("voice", source, "", command.strip(), carry, info), daemon=True,
                         name="session").start()
        return True

    def listen_now(self) -> None:
        self.activate_apolo("ui")

    def submit_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self._lock.acquire(blocking=False):
            self.wake.muted = True
            self.audio.set_sink(None)
            threading.Thread(target=self._session, args=("text", "ui", text), daemon=True, name="session").start()
        else:
            self.message_added.emit("system", "Estou ocupado no momento. Tente novamente em instantes.")

    def _session(self, mode: str, source: str, text: str, command: str = "", carry=None, info=None) -> None:
        try:
            self._wake_info = info
            if mode != "voice":
                self.audio.set_sink(None)
            if mode == "voice":
                self.audio.start()
                self.gemini.prewarm()  # reabre a conexão com o Gemini enquanto você ainda está falando
                self.music.duck(True)  # música local baixa enquanto o APOLO ouve e fala
                seed, respond = (b"" if source == "voice" else None), not command
                if not command and carry is not None and carry.speech:
                    # A pessoa já começou o comando (ex.: "Apolo ... toque Believer" com pausa): NÃO toca "Estou aqui"
                    # (a resposta cobriria o comando) e o áudio já ouvido entra direto na captura do comando.
                    seed, respond = carry.pcm, False
                    log.info("[WAKE] Comando já em andamento: reaproveitando %.0f ms de áudio, sem resposta de ativação",
                             len(carry.pcm) / 2 / 16.0)
                # Na ativação por voz, arme a captura antes de tocar a confirmação.
                self._activate(source, respond=respond and source != "voice")
                ready_feedback = self._play_voice_activation_cue if respond and source == "voice" else None
                direct = command       # "Apolo, <comando>": já temos o comando, não pede para repetir
                for turn_index in range(3):  # 1 pergunta + até 2 respostas de confirmação
                    self._begin_turn("voz+nome" if direct else "voz")
                    if direct:
                        heard, direct = direct, ""
                        self._note_listen(f"Comando recebido junto com o nome", heard)
                    else:
                        heard = self._listen(seed, activation=turn_index == 0, on_ready=ready_feedback)
                        ready_feedback = None
                        seed = None
                    if heard is None:
                        break
                    again = self._handle_text(heard)
                    self._end_turn()
                    if not again:
                        break
            else:
                self._begin_turn("texto")
                self._handle_text(text)
                self._end_turn()
        except Exception:
            log.exception("Erro na sessão")  # o erro real fica no log; a fala mantém a personalidade
            self._fail(self.gemini.persona.unexpected())
        finally:
            try:
                self.music.duck(False)
                self._set_state(State.IDLE)
                time.sleep(0.1 if self.cfg.wake_low_latency else 0.3)   # cauda do áudio de saída; o resto é a guarda adaptativa
                self._last_activation = time.monotonic()    # o cooldown conta a partir do FIM da sessão
                self._wake_info = None
                self._resume_listening()
            finally:
                self._lock.release()

    def _activate(self, source: str, respond: bool = True) -> None:
        self._set_state(State.ACTIVATED)
        self.banner.emit("CLAP DETECTED" if source == "clap" else "APOLO ONLINE")
        self.show_requested.emit()
        if respond:
            self._play_activation_response()
        self.banner.emit("APOLO ACTIVATED")

    def _play_activation_response(self) -> None:
        """Resposta curta PRÉ-RENDERIZADA, já em memória: nenhuma API/TTS no caminho. Sem os WAVs (ainda não gerados),
        cai nos dois tons de antes — nunca atrasa a ativação esperando síntese."""
        if self.cfg.activation_feedback == "silent":
            return
        if self.cfg.activation_feedback == "tone":
            from app.utils.sounds import activation_pcm
            self.tts.play_pcm(activation_pcm())
            self.tts.wait_idle(2)
            return
        clip = self.activation.pick()
        if clip is None:
            play_activation_sound(self.cfg.output_device)
            return
        log.info("[APOLO] Playing activation response: %s", clip.name)
        self.tts.play_pcm(clip.pcm)
        self.tts.wait_idle(10)

    def _play_voice_activation_cue(self) -> None:
        # Uma confirmação falada pode entrar no próprio gravador de comandos.
        # Chamadas por voz usam um sinal breve; palmas/botão mantêm a opção falada.
        if self.cfg.activation_feedback == "silent":
            return
        from app.utils.sounds import activation_pcm
        self.tts.play_pcm(activation_pcm())
        self.tts.wait_idle(2)

    def _begin_turn(self, label: str) -> None:
        self._turn = turn = Turn(label)
        self.tts.on_first_audio = lambda t: turn.mark("tts_first_audio", t)

    def _note_listen(self, what: str, heard: str | None = None) -> None:
        """Marca (uma vez por ativação) o instante em que o modo de comando começou e registra a latência."""
        info = self._wake_info
        if info is not None and info.t_listen is None:
            info.t_listen = time.perf_counter()
            if info.t_detect is not None:
                log.info("[COMMAND] %s (+%.0f ms após a detecção)", what, (info.t_listen - info.t_detect) * 1000)
        if heard is not None:
            log.info('[COMMAND] "%s"', heard)

    def _end_turn(self) -> None:
        if self._wake_info is not None:
            self._turn.wake = self._wake_info.as_dict()
            self._wake_info = None            # só o 1º turno da sessão pertence à ativação
        self._turn.mark("playback_end")
        self._turn.absorb_tts(getattr(self.tts, "timing", None))
        self._turn.log()

    def _listen(self, seed: bytes | None = None, activation: bool = False, on_ready=None) -> str | None:
        if activation and seed is None:
            seed = b""  # também consome áudio acumulado antes de a captura iniciar
        if seed is None:
            self.audio.flush()                 # com seed NÃO: o que já chegou pertence ao comando
        log.info("[APOLO] Listening for command")
        self._set_state(State.LISTENING)
        self._turn.mark("listen_start")
        self._note_listen("Captura iniciada" + (" (com áudio reaproveitado)" if seed else ""))
        # A palavra de ativação, sua cauda ou um ruído não devem fechar a
        # conversa. Uma nova janela silenciosa dá tempo para começar o pedido.
        unclear_retried = False
        for attempt in range(3 if activation else 1):
            self._set_state(State.LISTENING)
            try:
                extra = {"on_ready": on_ready} if on_ready is not None else {}
                on_ready = None  # não repetir confirmação nas tentativas silenciosas
                text = self.speech.listen(self.audio, self._turn,
                                          on_speech_end=lambda: self._set_state(State.PROCESSING),
                                          seed=seed, activation=activation, **extra)
                if text is None:
                    if not activation:
                        self.message_added.emit("system", 'Não ouvi nada. Diga “Apolo” ou clique em Falar para tentar de novo.')
                    return None
                if activation:
                    parsed = match_wake(text, self.cfg.wake_word)
                    if parsed.found:
                        text = parsed.command
                        if not text:
                            seed = b""  # retoma o backlog, sem apagar o começo do pedido
                            continue
                stt = self._turn.stt.get("total_s") if isinstance(self._turn.stt, dict) else None
                if stt:
                    log.info("[STT] %.0f ms", stt * 1000)
                log.info('[COMMAND] "%s"', text)
                return text
            except SpeechError as e:
                if activation and e.kind == "unclear" and not unclear_retried and attempt < 2:
                    unclear_retried = True
                    log.info("[COMMAND] Trecho inicial inconclusivo; aguardando o pedido sem interromper")
                    seed = b""
                    continue
                self._fail(e.message, speak=e.kind in ("unclear", "network", "stt"))
                return None
        return None

    # -------------------------------------------------------- entendimento
    def _handle_text(self, text: str) -> bool:
        """Processa uma frase. Devolve True se espera uma resposta falada (confirmação)."""
        self.message_added.emit("you", text)
        if self._pending:
            cmd, arg, ts = self._pending
            self._pending = None
            if time.time() - ts < 30 and is_yes(text):
                self._reply(self.commands.execute(cmd, arg))
            else:
                self._reply("Ok, cancelado.")
            return False

        try:
            reply = self.productivity.handle(text)
        except ProductivityError as exc:
            self._turn.kind = "produtividade"
            self._reply(str(exc))
            return False
        if reply is not None:
            self._turn.kind = "produtividade"
            self._reply(reply)
            self.refresh_productivity()
            return False

        reply = self.whatsapp.handle(text)
        if reply is not None:
            self._turn.kind = "whatsapp"
            self._reply(reply)
            return False

        if self.cfg.music_enabled:
            intent = self.music.parse(text)
            if intent is not None:
                self._turn.kind = "música"
                self._set_state(State.PROCESSING)
                t0 = time.perf_counter()
                reply = self.music.execute(intent)           # busca + inicia a reprodução DE VERDADE; devolve a frase curta
                self._turn.diag["cmd_exec_s"] = time.perf_counter() - t0
                self._reply(reply)
                return False

        parsed = self.commands.parse(text)
        if parsed:
            cmd, arg = parsed
            self._turn.kind = "comando"
            if cmd.dangerous:
                self._pending = (cmd, arg, time.time())
                self._reply(cmd.confirm)
                return True
            t0 = time.perf_counter()
            reply = self.commands.execute(cmd, arg)
            self._turn.diag["cmd_exec_s"] = time.perf_counter() - t0
            self._reply(reply)
            return False

        kind = intent_service.detect(text)
        self._turn.kind = {"time": "hora", "date": "data", "weather": "clima"}.get(kind or "", "gemini")
        if kind == "time":
            now = datetime.now()
            self._reply(f"{spoken_time(now)} {self.gemini.persona.late_night_quip(now, text)}".strip())
        elif kind == "date":
            self._reply(spoken_date(datetime.now()))
        elif kind == "weather":
            self._answer_weather()
        else:
            self._ask_gemini(text)
        return False

    def _answer_weather(self) -> None:
        self._set_state(State.PROCESSING)
        t0 = time.perf_counter()
        try:
            data = self.weather.fetch()
            self._turn.diag["weather_s"] = time.perf_counter() - t0
        except WeatherError as e:
            self._fail(f"Não consegui obter o clima agora. {e}")
            return
        self.weather_updated.emit(data)
        self._reply(WeatherService.spoken(data))

    def _ask_gemini(self, text: str) -> None:
        self._set_state(State.PROCESSING)
        prepare = getattr(self.tts, "prepare_output", None)
        if prepare:
            prepare()
        turn = self._turn
        turn.mark("stt_done")      # (texto digitado: sem STT; mantém o relatório coerente)
        turn.mark("gemini_sent")
        buf, full, started, spoken = "", "", False, False
        try:
            for delta in self.gemini.stream_reply(text):
                if not started:
                    started = True
                    turn.mark("first_token")
                    self.message_added.emit("apolo", "")
                    self._set_state(State.SPEAKING)
                full += delta
                buf += delta
                self.message_updated.emit(clean_for_display(full))
                sentences, buf = pop_sentences(buf, first=not spoken)
                for s in sentences:
                    if not spoken:
                        spoken = True
                        turn.mark("first_sentence")
                    self.tts.speak(clean_for_tts(s))
            if buf.strip():
                if not spoken:
                    turn.mark("first_sentence")
                self.tts.speak(clean_for_tts(buf))
            turn.mark("gemini_done")
            turn.diag["gemini"] = dict(self.gemini.last_info)
        except GeminiError as e:
            self.tts.stop()
            self.status_changed.emit("ai", "OFFLINE" if e.kind in ("network", "auth", "nokey", "server") else "ONLINE",
                                     "err" if e.kind in ("network", "auth", "nokey", "server") else "ok")
            self._fail(e.message)
            return
        if not started:
            self._reply("Desculpe, não consegui formular uma resposta para isso.")
            return
        self.status_changed.emit("ai", "ONLINE", "ok")
        self.tts.wait_idle(120)

    # ------------------------------------------------------------- saídas
    def _reply(self, text: str) -> None:
        self._turn.mark("reply_ready")
        self.message_added.emit("apolo", text)
        self._speak(text)

    def _speak(self, text: str) -> None:
        """Fala o texto FRASE A FRASE: a 1ª frase já sai enquanto as demais ainda estão sendo sintetizadas
        (antes o texto inteiro era sintetizado de uma vez antes de qualquer som)."""
        self._set_state(State.SPEAKING)
        if len(text) <= SPLIT_MIN_CHARS:
            parts = [text]        # frase curta: um único pedido (dividir criaria um "buraco" entre as partes)
        else:
            sentences, rest = pop_sentences(text.strip() + " ", first=True)
            parts = [p for p in sentences + ([rest] if rest.strip() else []) if p.strip()]
        for i, part in enumerate(parts):
            if i == 0:
                self._turn.mark("first_sentence")
            self.tts.speak(clean_for_tts(part))
        self.tts.wait_idle(120)

    def _fail(self, message: str, speak: bool = True) -> None:
        self.message_added.emit("system", message)
        self._set_state(State.ERROR)
        time.sleep(0.4)
        if speak:
            self._speak(message.replace("\n", " "))

    def _set_state(self, state: State) -> None:
        if state != self.state:
            log.debug("[APOLO] State: %s -> %s", PHASES.get(self.state, self.state.value), PHASES.get(state, state.value))
        self.state = state
        self.state_changed.emit(state.value)

    # ---------------------------------------------------------- utilidades
    def clear_history(self) -> None:
        self.gemini.reset()

    def apply_settings(self, new: Settings) -> None:
        old_device = self.cfg.input_device
        old_wake = (self.cfg.wake_model, self.cfg.whisper_device, self.cfg.whisper_compute)
        old_keyword = (self.cfg.wake_engine, self.cfg.wake_word, self.cfg.wake_keyword_threshold)
        for f in fields(Settings):
            setattr(self.cfg, f.name, getattr(new, f.name))
        self.cfg.save()
        self._sync_clap_cfg()
        autostart.set_enabled(self.cfg.start_with_windows)
        self.audio.device = self.cfg.input_device
        if old_wake != (self.cfg.wake_model, self.cfg.whisper_device, self.cfg.whisper_compute):
            self.wake.ready = False
            self.wake_stt.reset()
        if old_keyword != (self.cfg.wake_engine, self.cfg.wake_word, self.cfg.wake_keyword_threshold):
            self.wake.streaming.stop()
            self.wake.streaming = StreamingKeywordSpotter(self.cfg)
            self.wake.ready = False
        if self.cfg.wake_word_enabled and not self.wake.ready and not self.wake_stt.failed:
            threading.Thread(target=self._warm_wake, daemon=True, name="wake-warm").start()
        if old_device != self.cfg.input_device and self.audio._running:
            self.audio.restart()
        threading.Thread(target=self._refresh_weather, args=(True,), daemon=True).start()
        if not self._lock.locked():
            self._resume_listening()

    def calibrate_claps(self) -> None:
        if self._lock.acquire(blocking=False):
            threading.Thread(target=self._calibrate, daemon=True, name="calibrate").start()
        else:
            self.calibration_progress.emit("Aguarde o APOLO terminar o que está fazendo.")

    def _calibrate(self) -> None:
        try:
            self.audio.set_sink(None)
            if not self.audio.start():
                self.calibration_progress.emit("Microfone indisponível.")
                return
            cal = ClapCalibrator()
            self.audio.flush()
            self.audio.set_sink(cal.feed)
            self.calibration_progress.emit("1/2  Fique em silêncio por 3 segundos...")
            time.sleep(3)
            cal.phase = "claps"
            self.calibration_progress.emit("2/2  Bata palmas 3 vezes (5 segundos)...")
            time.sleep(5)
            self.audio.set_sink(None)
            value = cal.result()
            self.calibration_progress.emit(
                f"Sugerido: {value:.2f}  (ruído {cal.noise_floor:.2f}, {len(cal.clap_peaks)} palmas medidas)")
            self.calibration_done.emit(value)
        finally:
            self._resume_listening()
            self._lock.release()

    def shutdown(self) -> None:
        self._quit.set()
        worker = self._productivity_thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=3)
        # Sinais Qt podem continuar na fila quando a janela encerra.
        with self._productivity_notifications_lock:
            pending = [{"id": item_id} for item_id in self._productivity_pending]
        self._requeue_productivity(pending)
        self.productivity.close()
        self.stop_voice_test()
        self.wake.shutdown()
        self.wake_stt.stop()
        self.music.shutdown()
        self.tts.stop()
        self.audio.set_sink(None)
        self.audio.stop()
