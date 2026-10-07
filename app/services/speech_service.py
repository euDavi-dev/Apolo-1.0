"""Fala -> texto: VAD (webrtcvad) para delimitar a frase + Speech-to-Text.

Motores de STT (Configurações > Voz > Reconhecimento de fala):
  google   API gratuita do Google via SpeechRecognition. ONLINE. O áudio é codificado em FLAC por um programa
           externo (flac.exe) a cada chamada e enviado de uma vez, DEPOIS do fim da fala (não é streaming).
  gemini   envia o áudio ao Gemini (online; uma chamada de LLM com áudio).
  whisper  faster-whisper LOCAL (offline); o modelo fica carregado na memória. Opcional (requirements-local-stt.txt).
Cada transcrição devolve estatísticas (tamanho do áudio, tempo de FLAC, tempo de rede/servidor) para o log de latência.

Latência "eu terminei de falar -> APOLO começa a processar":
 - O fim da fala é confirmado após `end_silence_ms` (padrão 600 ms; antes eram 800 ms fixos).
 - Transcrição ESPECULATIVA: já em `soft_silence_ms` (320 ms) de silêncio a transcrição é disparada numa thread.
   Se a pessoa voltar a falar, o resultado é descartado; se o fim for confirmado, o resultado já está
   (quase) pronto. O áudio enviado é idêntico nos dois casos, então a precisão não muda.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
import wave
from collections import deque
from typing import Callable

import numpy as np

try:
    import webrtcvad  # pacote "webrtcvad-wheels"
except Exception:
    webrtcvad = None

from app.core.config import DATA_DIR, Settings
from app.services.audio_engine import RATE
from app.utils.metrics import Turn

log = logging.getLogger("apolo.speech")
FRAME = RATE * 20 // 1000  # 320 amostras = 20 ms


class SpeechError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class UtteranceCapture:
    """Recebe blocos de 20 ms e decide quando a pessoa começou e terminou de falar."""

    def __init__(self, aggressiveness: int = 2, start_timeout_s: float = 6.0,
                 end_silence_ms: int = 600, max_s: float = 15.0,
                 soft_silence_ms: int = 0,
                 on_soft_end: Callable[[bytes], None] | None = None,
                 on_resume: Callable[[], None] | None = None,
                 clock: Callable[[], float] | None = None,
                 pre_ms: int = 200, post_ms: int = 200, min_rms: float = 0.003,
                 min_voiced_ms: int = 0, start_voiced_ms: int = 120):
        self.clock = clock or time.perf_counter   # hora de CHEGADA do bloco que está sendo processado
        self.min_rms = max(0.0, min(0.03, min_rms))
        self.min_voiced_ms = min_voiced_ms
        self.start_voiced_frames = max(2, (start_voiced_ms + 19) // 20)
        self.vad = webrtcvad.Vad(max(0, min(3, aggressiveness))) if webrtcvad else None
        self.start_timeout_s = start_timeout_s
        self.end_silence_ms = end_silence_ms
        self.max_s = max_s
        self.soft_frames = (soft_silence_ms // 20) if 0 < soft_silence_ms < end_silence_ms else 0
        self.min_voiced_for_soft = 15          # 300 ms de voz antes de valer a pena especular
        self.on_soft_end = on_soft_end
        self.on_resume = on_resume
        # pré-roll: áudio ANTES de o VAD confirmar o início (ele precisa de 6 quadros com voz = 120 ms), para não cortar
        # a 1ª sílaba. pós-roll: silêncio mantido no fim do clip enviado ao STT. Com especulação, o pós-roll não pode
        # passar do silêncio "soft" (senão o clip especulativo e o final teriam tamanhos diferentes e seriam descartados).
        self.pre: deque = deque(maxlen=max(6, pre_ms // 20))
        self.post_frames = max(0, post_ms // 20)
        if self.soft_frames:
            self.post_frames = min(self.post_frames, self.soft_frames)
        self.frames: list[np.ndarray] = []
        self.triggered = False
        self.silence = 0
        self.voiced = 0
        self.soft_fired = False
        self.total = 0
        self.capture_frames = 0
        self.noise = 0.005
        self.done = threading.Event()
        self.result: bytes | None = None
        self.t_speech_start: float | None = None
        self.t_last_voice: float | None = None
        self.t_end: float | None = None
        self.end_reason = ""                   # "silêncio" | "limite de N s" | "sem fala"
        self.max_lag_s = 0.0                   # maior atraso (chegada -> processamento) visto durante a captura
        self.resumes = 0
        self._buf = np.zeros(0, dtype=np.int16)

    def _is_speech(self, frame: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean((frame.astype(np.float32) / 32768.0) ** 2)))
        if self.vad is not None:
            # O VAD pode chamar o ruído residual de "voz" e nunca encerrar a captura.
            # Ainda alimentamos seu estado interno; só descartamos energia quase inaudível.
            return self.vad.is_speech(frame.tobytes(), RATE) and rms >= self.min_rms
        speech = rms > max(0.02, self.noise * 3)
        if not speech:
            self.noise += 0.05 * (rms - self.noise)
        return speech

    def feed(self, block: np.ndarray) -> None:
        if self.done.is_set():
            return
        self._push(np.clip(block * 32767.0, -32768, 32767).astype(np.int16))

    def feed_int16(self, pcm: bytes) -> None:
        """Entrega áudio já capturado (int16, 16 kHz) sem conversões: usado para reaproveitar o que veio logo depois de
        'Apolo' e que ainda não pertencia a ninguém."""
        if pcm and not self.done.is_set():
            self._push(np.frombuffer(pcm, dtype=np.int16))

    def _push(self, pcm: np.ndarray) -> None:
        self._buf = np.concatenate([self._buf, pcm])
        while len(self._buf) >= FRAME and not self.done.is_set():
            frame, self._buf = self._buf[:FRAME], self._buf[FRAME:]
            self._frame(frame)

    def carry(self) -> tuple[bytes, bool, int]:
        """(pcm, parece_fala, quadros_com_voz) do que esta captura já viu e ainda não entregou a ninguém."""
        if self.triggered:
            frames, voiced = self.frames, self.voiced
        else:
            frames, voiced = [f for f, _ in self.pre], sum(1 for _, s in self.pre if s)
        pcm = np.concatenate(frames).tobytes() if frames else b""
        return pcm, bool(self.triggered or voiced >= 4), voiced

    def _frame(self, frame: np.ndarray) -> None:
        self.total += 1
        speech = self._is_speech(frame)
        secs = self.total * 0.02
        now = self.clock()
        self.max_lag_s = max(self.max_lag_s, time.perf_counter() - now)
        if not self.triggered:
            self.pre.append((frame, speech))
            if sum(1 for _, s in self.pre if s) >= self.start_voiced_frames:
                self.triggered = True
                self.t_speech_start = self.t_last_voice = now
                self.frames = [f for f, _ in self.pre]
                self.capture_frames = len(self.frames)
                self.voiced = sum(1 for _, s in self.pre if s)
                self.pre.clear()
            elif secs >= self.start_timeout_s:
                self._finish("sem fala")
            return
        self.capture_frames += 1
        self.frames.append(frame)
        if speech:
            self.silence = 0
            self.voiced += 1
            self.t_last_voice = now
            if self.soft_fired:
                self.soft_fired = False
                self.resumes += 1
                self._call(self.on_resume)
        else:
            self.silence += 1
            if (self.soft_frames and not self.soft_fired and self.silence >= self.soft_frames
                    and self.voiced >= self.min_voiced_for_soft):
                self.soft_fired = True
                self._call(self.on_soft_end, self._snapshot())
        if self.silence * 20 >= self.end_silence_ms:
            if self.voiced * 20 < self.min_voiced_ms:
                # Estalo/cauda do sinal não é um pedido. Rearma sem renovar o
                # prazo total de espera e sem enviar esse ruído ao reconhecedor.
                self.triggered = False
                self.frames.clear()
                self.pre.clear()
                self.voiced = self.silence = self.capture_frames = 0
                self.t_speech_start = self.t_last_voice = None
                return
            self._finish("silêncio", now)
        elif self.capture_frames * 0.02 >= self.max_s:
            self._finish(f"limite de {self.max_s:g} s", now)

    def _snapshot(self) -> bytes:
        """PCM da fala com 200 ms de silêncio no fim — idêntico ao resultado final se ninguém voltar a falar."""
        trim = max(0, self.silence - self.post_frames)
        frames = self.frames[:-trim] if trim else self.frames
        return np.concatenate(frames).tobytes()

    @staticmethod
    def _call(fn, *args) -> None:
        if fn is not None:
            try:
                fn(*args)
            except Exception:
                log.exception("Callback do VAD falhou")

    def _finish(self, reason: str = "", now: float | None = None) -> None:
        if self.triggered and self.frames:
            self.result = self._snapshot()
        self.end_reason = reason
        self.t_end = self.clock() if now is None else now
        self.done.set()

    def stats(self) -> dict:
        """Resumo da captura para o log de latência."""
        audio_s = (len(self.result) / 2 / RATE) if self.result else 0.0
        return {"fim_por": self.end_reason or "?", "audio_s": audio_s, "voiced_s": self.voiced * 0.02,
                "lag_max_ms": self.max_lag_s * 1000.0, "resumes": self.resumes}


def pcm_to_wav(pcm: bytes) -> bytes:
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return bio.getvalue()


class _Attempt:
    """Uma transcrição especulativa em andamento."""

    def __init__(self, pcm_len: int):
        self.pcm_len = pcm_len
        self.valid = True
        self.done = threading.Event()
        self.value: str | None = None
        self.error: Exception | None = None
        self.stats: dict = {}
        self.t_start = time.perf_counter()


class SpeechService:
    def __init__(self, cfg: Settings, gemini=None):
        self.cfg = cfg
        self.gemini = gemini

    # ---- captura -----------------------------------------------------------
    def capture(self, engine) -> bytes | None:
        """Bloqueia até a pessoa terminar de falar. Devolve PCM 16 kHz int16 ou None (silêncio)."""
        if not engine.ok:
            raise SpeechError("mic", "Não consegui acessar o microfone.")
        cap = self._new_capture()
        with engine.use_sink(cap.feed):
            cap.done.wait(timeout=cap.start_timeout_s + cap.max_s + 2)
        return cap.result

    def _new_capture(self, **extra) -> UtteranceCapture:
        c = self.cfg
        return UtteranceCapture(aggressiveness=c.vad_aggressiveness, end_silence_ms=c.vad_end_silence_ms,
                                min_rms=c.vad_min_rms, **extra)

    def listen(self, engine, turn: Turn | None = None,
               on_speech_end: Callable[[], None] | None = None, seed: bytes | None = None,
               activation: bool = False, on_ready: Callable[[], None] | None = None) -> str | None:
        """Captura + transcrição (com especulação). Devolve o texto, ou None se ninguém falou.

        Levanta SpeechError. `on_speech_end` é chamado assim que o fim da fala é confirmado.
        `seed`: áudio que já tinha sido ouvido (ex.: o começo do comando dito logo após "Apolo") — entra na captura
        antes de qualquer bloco novo, junto com o backlog do motor de áudio, então nada do comando se perde."""
        if not engine.ok:
            raise SpeechError("mic", "Não consegui acessar o microfone.")
        attempt: list[_Attempt | None] = [None]
        lock = threading.Lock()

        def run(pcm: bytes, at: _Attempt) -> None:
            try:
                at.value = self.transcribe(pcm, at.stats)
            except Exception as e:  # inclusive SpeechError("unclear")
                at.error = e
            finally:
                at.done.set()

        def soft_end(pcm: bytes) -> None:
            at = _Attempt(len(pcm))
            with lock:
                if attempt[0] is not None and not attempt[0].done.is_set():
                    return  # uma pausa não cria várias requisições simultâneas
                attempt[0] = at
            threading.Thread(target=run, args=(pcm, at), daemon=True, name="stt-spec").start()

        def resume() -> None:
            with lock:
                if attempt[0] is not None:
                    attempt[0].valid = False

        speculative = bool(self.cfg.stt_speculative) and self.cfg.vad_soft_silence_ms > 0
        clock = (lambda: engine.block_ts) if hasattr(engine, "block_ts") else None
        cap = self._new_capture(soft_silence_ms=self.cfg.vad_soft_silence_ms if speculative else 0,
                                on_soft_end=soft_end if speculative else None,
                                on_resume=resume if speculative else None, clock=clock,
                                start_timeout_s=8.0 if activation else 6.0,
                                min_voiced_ms=180 if activation else 0)
        if activation:
            cap.end_silence_ms = max(800, cap.end_silence_ms)
        if seed:
            cap.feed_int16(seed)
        if activation and cap.done.is_set() and len(cap._buf):
            engine.buffer_block(cap._buf.astype(np.float32) / 32768.0, prepend=True)
            cap._buf = np.zeros(0, dtype=np.int16)

        def feed_command(block):
            if cap.done.is_set():
                engine.buffer_block(block)
            else:
                cap.feed(block)

        sink_cm = engine.use_sink(feed_command, backlog=seed is not None, retain_backlog=True) if activation else (
            engine.use_sink(cap.feed, backlog=True) if seed is not None else engine.use_sink(cap.feed))
        with sink_cm:
            # A confirmação só sai depois que o microfone já entrega ao gravador.
            # Quem fala durante o sinal não perde o início do pedido.
            if on_ready is not None and not cap.triggered and not cap.done.is_set():
                on_ready()
            cap.done.wait(timeout=cap.start_timeout_s + cap.max_s + 2)
        pcm = cap.result
        if pcm is None:
            return None
        if turn:
            turn.mark("speech_start", cap.t_speech_start)
            turn.mark("voice_end", cap.t_last_voice)
            turn.mark("speech_end", cap.t_end)
            turn.mark("capture_done")
            turn.cap = {**cap.stats(), "dropped": getattr(engine, "dropped", 0)}
        self._debug_save(pcm)
        if on_speech_end:
            on_speech_end()

        with lock:
            at = attempt[0]
        mode = "completa (sem especulação)" if not speculative else "especulativa não chegou a disparar"
        if at is not None and at.valid and at.pcm_len == len(pcm):
            at.done.wait(timeout=15)
            if at.error is None and at.value is not None:
                self._note_stt(turn, at.stats, "especulativa aproveitada", at.t_start, at.t_start + at.stats.get("total_s", 0))
                return at.value
            if isinstance(at.error, SpeechError) and at.error.kind == "unclear":
                raise at.error
            log.info("STT especulativa falhou (%r); repetindo normalmente", at.error)
            mode = "especulativa falhou → repetida"
        elif at is not None:
            log.info("STT especulativa descartada (a pessoa continuou falando)")
            mode = "especulativa descartada → repetida"
        t_start = time.perf_counter()
        stats: dict = {}
        text = self.transcribe(pcm, stats)
        self._note_stt(turn, stats, mode, t_start, None)
        return text

    @staticmethod
    def _note_stt(turn: Turn | None, stats: dict, mode: str, t_start: float, t_end: float | None) -> None:
        if not turn:
            return
        stats["mode"] = mode
        turn.stt = stats
        turn.mark("stt_start", t_start)
        turn.mark("stt_done")

    def _debug_save(self, pcm: bytes) -> None:
        """APOLO_DEBUG_AUDIO=1: guarda a última captura em %APPDATA%\\APOLO\\last_utterance.wav para você OUVIR
        exatamente o que foi enviado ao STT (útil para achar ruído, cortes ou captura longa demais)."""
        if os.environ.get("APOLO_DEBUG_AUDIO", "").strip() not in ("1", "true", "sim"):
            return
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / "last_utterance.wav").write_bytes(pcm_to_wav(pcm))
        except OSError:
            log.exception("Não consegui salvar a última captura")

    # ---- transcrição ---------------------------------------------------------
    def transcribe(self, pcm: bytes, stats: dict | None = None) -> str:
        """PCM int16 16 kHz -> texto. `stats` (opcional) recebe: engine, audio_s, pcm_kb, total_s e, no Google,
        flac_s / flac_kb (preparo do áudio) e net_s (upload + servidor + resposta)."""
        st = stats if stats is not None else {}
        engine = self.cfg.stt_engine
        st.update(engine=engine, audio_s=len(pcm) / 2 / RATE, pcm_kb=len(pcm) / 1024.0)
        t0 = time.perf_counter()
        try:
            if engine == "gemini" and self.gemini is not None:
                try:
                    text = self.gemini.transcribe(pcm_to_wav(pcm), self.cfg.language)
                except Exception as e:
                    raise SpeechError("stt", getattr(e, "message", str(e))) from e
            elif engine == "whisper":
                text = self._whisper(pcm, st)
            else:
                st["engine"] = "google"
                text = self._google(pcm, st)
        finally:
            st["total_s"] = time.perf_counter() - t0
            if "flac_s" in st:
                st["net_s"] = max(0.0, st["total_s"] - st["flac_s"])
        if not text.strip():
            raise SpeechError("unclear", self._unclear_msg())
        return text.strip()

    @staticmethod
    def _unclear_msg() -> str:
        from app.core.persona import shared
        return shared.unclear()

    # -- Google (online, gratuito)
    def _google(self, pcm: bytes, st: dict) -> str:
        try:
            import speech_recognition as sr
        except Exception as e:
            raise SpeechError("stt", "Biblioteca SpeechRecognition não instalada.") from e
        r = sr.Recognizer()
        r.operation_timeout = 10
        audio = _timed_audio_class(sr)(pcm, RATE, 2)
        audio.stats = st
        try:
            return r.recognize_google(audio, language=self.cfg.language)
        except sr.UnknownValueError:
            raise SpeechError("unclear", self._unclear_msg())
        except sr.RequestError as e:
            log.warning("Falha no reconhecimento do Google: %r", e)
            raise SpeechError("network", "Não consegui acessar o reconhecimento de voz. "
                                         "Verifique sua conexão com a internet.") from e

    # -- faster-whisper (local, offline)
    _whisper_model = None
    _whisper_lock = threading.Lock()

    def warm(self) -> None:
        """Carrega o modelo local na memória (chamado no boot quando o motor é 'whisper'). Nunca levanta erro."""
        if self.cfg.stt_engine != "whisper":
            return
        try:
            self._load_whisper()
            self._whisper(np.zeros(RATE, dtype=np.int16).tobytes(), {})     # 1ª inferência (inicialização preguiçosa)
        except Exception as e:
            log.warning("Não consegui aquecer o faster-whisper: %r", e)

    def _load_whisper(self):
        with SpeechService._whisper_lock:
            if SpeechService._whisper_model is None:
                os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")     # aviso inofensivo no Windows
                try:
                    from faster_whisper import WhisperModel
                except Exception as e:
                    raise SpeechError("stt", "O reconhecimento local precisa do pacote faster-whisper "
                                             "(pip install -r requirements-local-stt.txt).") from e
                t0 = time.perf_counter()
                c = self.cfg
                SpeechService._whisper_model = WhisperModel(c.whisper_model, device=c.whisper_device,
                                                            compute_type=c.whisper_compute)
                log.info("faster-whisper '%s' (%s/%s) carregado em %.1fs", c.whisper_model, c.whisper_device,
                         c.whisper_compute, time.perf_counter() - t0)
            return SpeechService._whisper_model

    def _whisper(self, pcm: bytes, st: dict) -> str:
        model = self._load_whisper()
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        lang = (self.cfg.language or "pt").split("-")[0].lower()
        try:
            segments, _info = model.transcribe(audio, language=lang, beam_size=1, vad_filter=False,
                                               condition_on_previous_text=False)
            return " ".join(seg.text.strip() for seg in segments)       # o gerador só trabalha aqui
        except SpeechError:
            raise
        except Exception as e:
            log.warning("faster-whisper falhou: %r", e, exc_info=True)
            raise SpeechError("stt", "O reconhecimento local falhou. Veja o log para detalhes.") from e


_TIMED_CLS = None


def _timed_audio_class(sr):
    """AudioData que mede o preparo do áudio: a lib chama get_flac_data() (que executa o flac.exe) a cada transcrição."""
    global _TIMED_CLS
    if _TIMED_CLS is None:
        class TimedAudio(sr.AudioData):
            stats: dict = {}

            def get_flac_data(self, *a, **k):
                t = time.perf_counter()
                data = super().get_flac_data(*a, **k)
                self.stats["flac_s"] = self.stats.get("flac_s", 0.0) + (time.perf_counter() - t)
                self.stats["flac_kb"] = len(data) / 1024.0
                return data
        _TIMED_CLS = TimedAudio
    return _TIMED_CLS
