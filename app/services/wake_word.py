"""Ativação local híbrida: VAD -> detector contínuo + faster-whisper em português.

Threads (cada uma faz uma coisa e nenhuma espera pela outra):
  1. PortAudio callback  : só copia o bloco de 20 ms para uma fila (audio_engine.py)
  2. consumidor          : entrega o bloco ao sink de standby -> aqui: VAD (barato) e detector de palmas
  3. wake-stt (1 worker) : transcrição (faster-whisper), inclusive a ESPECULATIVA; o resultado volta por callback
  4. wake-events         : entrega ativações fora das travas do microfone
  5. keyword-stream     : detector fonético contínuo; reconhece o nome enquanto o áudio chega
  6. sessão              : criada na ativação; recebe o áudio preservado da chamada e do pedido

Fluxo:
    bloco ─► UtteranceCapture (VAD + pré-roll de WAKE_WORD_PRE_BUFFER_MS)      só há trabalho pesado quando HÁ FALA
                ├─ silêncio curto (soft)  ─► STT especulativo (descartado se a pessoa continuar)
                └─ fim da frase           ─► usa o STT pronto ─► match_wake() ─► confiança ≥ mínimo? ─► on_wake()
    Enquanto o STT roda, a captura SEGUINTE já está gravando; na ativação ela é entregue à sessão (take_carry) —
    se a pessoa já começou o comando, nada se perde e a resposta "Estou aqui" nem é tocada.

Confiança (0..1) = similaridade do token com "apolo" × fator de posição × (0,55 + 0,45 × confiança do decodificador),
com penalidade para "Apolo" sozinho dentro de uma fala longa. Só vale no INÍCIO da frase (após até 3 palavras de
cortesia: "ei", "hey", "bom dia"). Citar o nome no meio de uma conversa não ativa.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from collections import deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Callable

import numpy as np

from app.core.config import Settings
from app.services.audio_engine import BLOCK_MS, RATE
from app.services.speech_service import UtteranceCapture
from app.utils.textutils import normalize

log = logging.getLogger("apolo.wake")


# ----------------------------------------------------------------------------------------- casamento do texto
_FILLERS = frozenset({"ei", "ey", "hey", "hi", "hello", "yo", "oi", "ola", "opa", "ok", "okay", "e", "ai", "fala", "bom",
                      "boa", "dia", "tarde", "noite", "senhor", "salve"})
_ALIASES = frozenset({"apollo", "apolu"})
_NEVER = frozenset({"apoio", "apelo", "polo", "paulo", "bolo", "rolo"})  # parecidas, mas não são
_LEAD_MAX = 3                 # palavras de cortesia permitidas antes do nome
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_TRIM = " \t\r\n,;:.!?…–—-\"'“”‘’"
WAKE_ONLY_MAX_VOICED_S = 2.5  # "Apolo" sozinho dura ~0,5 s; muita voz com só "Apolo" no texto é suspeito (alucinação)


@dataclass(frozen=True)
class WakeMatch:
    found: bool
    command: str = ""         # o que veio depois do nome ("" se só disseram "Apolo")
    token: str = ""           # trecho reconhecido como o nome
    score: float = 0.0        # similaridade × fator de posição (0..1); ainda sem a confiança do decodificador


def _sim(tok: str, target: str, threshold: float) -> float:
    """Similaridade (0..1) entre o token falado e a palavra de ativação; 0 se não parece."""
    if not tok or tok in _NEVER or any(c.isdigit() for c in tok) or not (3 <= len(tok) <= len(target) + 3):
        return 0.0
    if tok == target:
        return 1.0
    if target == "apolo" and tok in _ALIASES:
        return 0.92
    r = SequenceMatcher(None, tok, target).ratio()
    return r if r >= threshold else 0.0


def match_wake(text: str, wake_word: str = "apolo", threshold: float = 0.78) -> WakeMatch:
    """Procura a palavra de ativação no INÍCIO de `text`. Preserva acentos/pontuação no comando devolvido."""
    target = normalize(wake_word)
    if not text or not target:
        return WakeMatch(False)
    spans = [(m.group(), m.start(), m.end()) for m in _TOKEN.finditer(text)]
    norms = [normalize(t) for t, _, _ in spans]
    hit_end, hit_tok, score = None, "", 0.0
    for i in range(min(len(spans), _LEAD_MAX + 1)):
        if any(n not in _FILLERS for n in norms[:i]):
            break                                              # algo que não é cortesia antes do nome
        sc = _sim(norms[i], target, threshold)
        tok, end = spans[i][0], spans[i][2]
        if not sc and i + 1 < len(spans) and len(norms[i]) <= 4 and len(norms[i + 1]) <= 4:   # "a polo", "a pólo"
            sc = _sim(norms[i] + norms[i + 1], target, max(threshold, 0.85))
            tok, end = f"{spans[i][0]} {spans[i + 1][0]}", spans[i + 1][2]
        if sc:
            hit_end, hit_tok, score = end, tok, sc * (1.0 - 0.03 * i)       # cada palavra de cortesia custa 3%
            break
    if hit_end is None:
        return WakeMatch(False)
    command = text[hit_end:].lstrip(_TRIM).rstrip()
    rest = [normalize(m_.group()) for m_ in _TOKEN.finditer(command)]
    if not rest or all(_sim(n, target, threshold) for n in rest):      # "Apolo, Apolo" = só chamou
        command = ""
    return WakeMatch(True, command, hit_tok, score)


def wake_confidence(m: WakeMatch, decoder_conf: float = 1.0, voiced_s: float = 0.0) -> float:
    """Confiança final 0..1. decoder_conf: exp(avg_logprob) do Whisper (1.0 = desconhecida)."""
    if not m.found:
        return 0.0
    c = m.score * (0.55 + 0.45 * max(0.0, min(1.0, decoder_conf)))
    if not m.command and voiced_s > WAKE_ONLY_MAX_VOICED_S:
        c *= 0.6
    return c


# ----------------------------------------------------------------------------------------- dados
@dataclass
class Heard:
    """Resultado do STT do wake word."""
    text: str
    confidence: float = 1.0          # 0..1 (exp da média dos avg_logprob); 1.0 = desconhecida (dublês de teste)
    no_speech: float = 0.0


@dataclass
class Carry:
    """Áudio que chegou DEPOIS da frase de ativação e ainda não pertencia a ninguém."""
    pcm: bytes = b""
    speech: bool = False             # já parece fala (a pessoa começou o comando)
    voiced_frames: int = 0


@dataclass
class WakeInfo:
    """Linha do tempo de UMA ativação por voz (todos os tempos em time.perf_counter(); None = não medido)."""
    text: str = ""
    command: str = ""
    token: str = ""
    confidence: float = 0.0
    voiced_s: float = 0.0
    spec_used: bool = False                      # a transcrição especulativa foi aproveitada
    partial: bool = False                        # reconhecimento antecipado: comando virá do áudio completo
    capture_id: int | None = None                # fronteira do áudio pertencente à chamada
    t_voice_start: float | None = None           # 1º bloco com voz (hora de chegada do áudio)
    t_voice_end: float | None = None             # último bloco com voz
    t_vad_end: float | None = None               # o VAD confirmou o fim da frase
    t_stt_start: float | None = None
    t_stt_end: float | None = None
    t_detect: float | None = None                # "Apolo" reconhecido e aprovado
    t_activate: float | None = None              # activate_apolo aceitou
    t_listen: float | None = None                # captura do comando começou (ou comando já em mãos)
    t_stt2_end: float | None = None

    @staticmethod
    def _d(a, b):
        return None if a is None or b is None else max(0.0, b - a)

    @property
    def latency_s(self):            # fim da fala → detecção: é o que a pessoa sente
        return self._d(self.t_voice_end, self.t_detect)

    @property
    def vad_wait_s(self):           # silêncio que o VAD esperou para confirmar o fim
        return self._d(self.t_voice_end, self.t_vad_end)

    @property
    def stt_s(self):
        return self._d(self.t_stt_start, self.t_stt_end)

    @property
    def listen_after_s(self):       # detecção → captura do comando pronta
        return self._d(self.t_detect, self.t_listen)

    def as_dict(self) -> dict:
        return {"latency_s": self.latency_s, "vad_wait_s": self.vad_wait_s, "stt_s": self.stt_s,
                "listen_after_s": self.listen_after_s, "confidence": self.confidence, "spec_used": self.spec_used}


# ----------------------------------------------------------------------------------------- STT local
class WhisperWakeSTT:
    """faster-whisper para a palavra de ativação. Reaproveita o modelo do STT principal quando é o mesmo."""

    def __init__(self, cfg: Settings, shared_loader: Callable | None = None):
        self.cfg = cfg
        self.shared_loader = shared_loader
        self._model = None
        self._lock = threading.Lock()               # carga do modelo
        self._run_lock = threading.Lock()           # uma inferência real por vez; não roda inferências periódicas de silêncio
        self._stop = threading.Event()
        self._last_use = 0.0
        self._kwargs = {"max_new_tokens": 96}       # preserva comandos ditos junto com o nome
        self.error = ""
        self.failed = False
        self.load_s = 0.0
        self.effective_model = cfg.wake_model
        self.last_infer_s = 0.0
        self.infer_times: list[float] = []

    def reset(self) -> None:
        with self._lock:
            self._model, self.failed, self.error = None, False, ""

    def stop(self) -> None:
        self._stop.set()

    def load(self) -> bool:
        """Carrega e AQUECE o modelo (a 1ª inferência é bem mais lenta). Nunca levanta; veja .error."""
        with self._lock:
            if self._model is not None:
                return True
            t0 = time.perf_counter()
            c = self.cfg
            self.effective_model = c.wake_model
            try:
                if self.shared_loader is not None and c.stt_engine == "whisper" and c.whisper_model == c.wake_model:
                    self._model = self.shared_loader()
                else:
                    try:
                        from faster_whisper import WhisperModel
                    except ImportError as e:
                        raise RuntimeError("A ativação por voz precisa do faster-whisper "
                                           "(pip install -r requirements-local-stt.txt).") from e
                    # baixa latência: todas as threads do CTranslate2 (0 = padrão); econômico: só 2, para não competir com o resto
                    options = dict(device=c.whisper_device, compute_type=c.whisper_compute,
                                   cpu_threads=0 if c.wake_low_latency else 2)
                    self.effective_model = c.wake_model
                    try:
                        self._model = WhisperModel(c.wake_model, **options)
                    except Exception:
                        if c.wake_model != "tiny":
                            raise
                        # A atualização deve continuar funcionando offline se o base antigo já existe.
                        self._model = WhisperModel("base", local_files_only=True, **options)
                        self.effective_model = "base"
                        log.warning("[WAKE] tiny indisponível; usando base já salvo neste computador. "
                                    "Conecte à internet e reinicie para baixar o modelo mais leve.")
                noise = (np.random.default_rng(1).standard_normal(RATE) * 0.01).astype(np.float32)
                with self._run_lock:
                    self._infer(noise)  # aquece uma vez; depois a CPU fica disponível para fala real
            except Exception as e:
                self._model, self.failed = None, True
                self.error = (f"Ativação por voz indisponível: {e}" if isinstance(e, RuntimeError)
                              else f"Ativação por voz indisponível: não consegui carregar o modelo '{c.wake_model}' ({e}). "
                                   "Na 1ª vez o download precisa de internet.")
                log.warning("[APOLO] %s", self.error)
                return False
            self.load_s = time.perf_counter() - t0
            self._last_use = time.monotonic()
            log.info("[WAKE] Modelo '%s' carregado e aquecido em %.1fs", self.effective_model, self.load_s)
            return True

    def _infer(self, audio: np.ndarray, locked: bool = False) -> Heard:
        lang = (self.cfg.language or "pt").split("-")[0].lower()
        kw = dict(language=lang, beam_size=1, temperature=0.0, vad_filter=False, without_timestamps=True,
                  condition_on_previous_text=False, initial_prompt=f"Assistente {self.cfg.wake_word.capitalize()}.")
        t0 = time.perf_counter()
        try:
            segments, _ = self._model.transcribe(audio, **kw, **self._kwargs)
            segs = list(segments)
        except TypeError:                                   # versão antiga sem max_new_tokens
            self._kwargs = {}
            segments, _ = self._model.transcribe(audio, **kw)
            segs = list(segments)
        self.last_infer_s = time.perf_counter() - t0
        self._last_use = time.monotonic()
        good = [s for s in segs if getattr(s, "no_speech_prob", 0.0) < 0.6]
        if not good:
            return Heard("", 0.0, max((getattr(s, "no_speech_prob", 0.0) for s in segs), default=1.0))
        w = [max(0.1, getattr(s, "end", 1.0) - getattr(s, "start", 0.0)) for s in good]
        lp = sum(getattr(s, "avg_logprob", -0.3) * wi for s, wi in zip(good, w)) / sum(w)
        return Heard(" ".join(s.text.strip() for s in good).strip(), max(0.0, min(1.0, math.exp(lp))),
                     max(getattr(s, "no_speech_prob", 0.0) for s in good))

    def __call__(self, pcm: bytes) -> Heard:
        if self._model is None and not self.load():
            return Heard("", 0.0)
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        # O Whisper perde palavras curtas/baixas com mais facilidade. Normaliza
        # somente o trecho aprovado pelo VAD e acrescenta contexto silencioso.
        if len(audio):
            audio -= np.mean(audio)
            rms = float(np.sqrt(np.mean(audio * audio)))
            peak = float(np.max(np.abs(audio)))
            if rms > 1e-6 and peak > 0:
                audio *= min(6.0, 0.06 / rms, 0.95 / peak)
            audio = np.pad(audio, (RATE // 5, RATE // 5))
        with self._run_lock:
            h = self._infer(audio, locked=True)
        self.infer_times.append(self.last_infer_s)
        del self.infer_times[:-200]
        return h


# ----------------------------------------------------------------------------------------- detector contínuo
class _Job:
    __slots__ = ("pcm_len", "valid", "future", "t_start", "t_end", "epoch")

    def __init__(self, pcm_len: int, future: Future):
        self.pcm_len, self.valid, self.future = pcm_len, True, future
        self.t_start = self.t_end = None
        self.epoch = 0


class WakeWordDetector:
    """feed(bloco) roda na thread do microfone (só VAD, barato). O STT roda numa thread própria.

    transcribe(pcm_int16_16k) -> str | Heard      (normalmente WhisperWakeSTT)
    on_wake(comando, texto_ouvido, WakeInfo) -> bool   UMA vez por frase que começa com a palavra de ativação."""

    MIN_VOICED_S = 0.16           # permite chamadas breves, ainda filtradas pelo VAD/confiança
    CARRY_MAX_BYTES = RATE * 2 * 8  # 8 s: preserva também comandos concluídos durante o STT

    def __init__(self, cfg: Settings, transcribe: Callable[[bytes], "str | Heard"],
                 on_wake: Callable[[str, str, WakeInfo], bool], clock: Callable[[], float] = time.monotonic,
                 audio_clock: Callable[[], float] | None = None):
        self.cfg = cfg
        self.transcribe = transcribe
        self.on_wake = on_wake
        self.clock = clock                          # relógio do debounce
        self.audio_clock = audio_clock              # hora de CHEGADA do bloco (AudioEngine.block_ts); None = hora do processamento
        self.ready = False                          # modelo carregado (quem carrega marca True)
        self.muted = False                          # sessão em andamento: ignora o microfone
        self.is_busy: Callable[[], bool] | None = None   # ex.: TTS falando
        self.noisy: Callable[[], bool] | None = None     # ex.: música tocando (limita frases longas enviadas ao STT)
        self.streaming = None                       # detector contínuo; fallback STT permanece independente
        self.on_heard = None                        # diagnóstico ao vivo, sem persistir gravações
        self.stats = {"utterances": 0, "transcribed": 0, "activations": 0, "debounced": 0, "dropped": 0,
                      "low_conf": 0, "music_skipped": 0, "carry_used": 0}
        self.last_stt_s = 0.0
        self.history: list[WakeInfo] = []           # últimas ativações (benchmark/diagnóstico)
        self._cap: UtteranceCapture | None = None
        self._prefix_capture: UtteranceCapture | None = None
        self._completed: deque = deque()
        self._capture_serial = 0
        self._handoff_id = None
        self._handoff_partial = False
        self._spec: _Job | None = None
        self._flock = threading.RLock()             # feed x take_carry (RLock: on_wake pode rodar dentro do feed)
        self._inflight = 0
        self._last_fire = -1e9
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="wake-stt")
        self._events = ThreadPoolExecutor(1, thread_name_prefix="wake-events")
        self._epoch = 0
        # guarda de eco pós-fala (adaptativa)
        self._guard_left = 0
        self._guard_min = 0
        self._guard_elapsed = 0
        self._quiet_run = 0
        self._noise = 0.005                         # ruído ambiente (média móvel do RMS dos blocos sem voz)

    # ---- controle ----------------------------------------------------------------------------------
    def reset(self, guard_s: float = 0.0, min_guard_s: float | None = None, rearm: bool = False) -> None:
        """Descarta a frase em andamento e ignora o microfone por até `guard_s` (a cauda do áudio do APOLO).

        Modo baixa latência: a guarda termina ANTES se o nível já voltou ao ruído ambiente (≥ min_guard_s e 120 ms
        seguidos silenciosos) — eco real tem energia bem acima do ambiente; no modo econômico vale o tempo cheio."""
        with self._flock:
            self._epoch += 1
            self._cap = None
            self._prefix_capture = None
            self._completed.clear()
            self._handoff_id = None
            self._invalidate()
            self._guard_left = int(max(0.0, guard_s) * 1000 / BLOCK_MS)
            m = self.cfg.wake_guard_min_s if min_guard_s is None else min_guard_s
            self._guard_min = int(m * 1000 / BLOCK_MS) if self.cfg.wake_low_latency else self._guard_left
            self._guard_elapsed = self._quiet_run = 0
            if rearm:
                with self._lock:
                    self._last_fire = min(self._last_fire, self.clock() - self.cfg.activation_cooldown_s + self.cfg.wake_rearm_s)

    def take_carry(self) -> Carry:
        """Cala o detector e entrega o que a captura em andamento já ouviu depois da frase de ativação.
        Chamar DEPOIS de AudioEngine.set_sink(None, keep_backlog=True) para a troca não perder nenhum bloco."""
        with self._flock:
            self.muted = True
            cap, self._cap = self._cap, None
            prefix, self._prefix_capture = self._prefix_capture, None
            self._invalidate()
            if self._handoff_id is not None:
                # Uma segunda frase pode terminar enquanto a primeira chamada
                # ainda está sendo reconhecida. Ela também pertence ao pedido.
                pieces = [item[1] for item in self._completed
                          if item[0] > self._handoff_id or
                          (self._handoff_partial and item[0] == self._handoff_id)]
                if cap is not None and (cap.wake_capture_id > self._handoff_id or self._handoff_partial):
                    pieces.append(Carry(*cap.carry()))
                pcm = b"".join(piece.pcm for piece in pieces)
                speech = any(piece.speech for piece in pieces)
                voiced = sum(piece.voiced_frames for piece in pieces)
            else:
                pcm, speech, voiced = cap.carry() if cap is not None else (b"", False, 0)
            if self._handoff_id is None and prefix is not None and prefix is not cap:
                # O fim da frase pode chegar entre o reconhecimento e a troca do sink.
                # Conserva também essa captura, mesmo se a seguinte já começou.
                before, before_speech, before_voiced = prefix.carry()
                pcm, speech, voiced = before + pcm, before_speech or speech, before_voiced + voiced
            self._completed.clear()
            self._handoff_id = None
            if speech:
                self.stats["carry_used"] += 1
            return Carry(pcm, speech, voiced)

    def flush(self, timeout: float = 5.0) -> None:
        """Espera os jobs de STT em andamento (usado em testes)."""
        if self.streaming is not None:
            self.streaming.flush(timeout)
        self._pool.submit(lambda: None).result(timeout)
        self._events.submit(lambda: None).result(timeout)

    def shutdown(self) -> None:
        self.ready = False
        self._epoch += 1
        if self.streaming is not None:
            self.streaming.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._events.shutdown(wait=False, cancel_futures=True)

    def _dispatch(self, fn, *args) -> None:
        # Nunca ativa enquanto a thread do microfone segura a trava do sink/VAD.
        # Mesmo um Future já concluído precisa passar pela fila de eventos.
        try:
            self._events.submit(fn, *args)
        except RuntimeError:
            pass  # encerramento do aplicativo

    # ---- áudio -------------------------------------------------------------------------------------
    def feed(self, block: np.ndarray) -> None:
        with self._flock:
            if (not self.cfg.wake_word_enabled or not self.ready or self.muted
                    or (self.is_busy is not None and self.is_busy())):
                if self._cap is not None:              # estava no meio de uma frase: descarta
                    self._cap = None
                    self._invalidate()
                return
            rms = float(np.sqrt(np.mean(block * block))) if len(block) else 0.0
            if self._guard_left > 0:
                self._guard_left -= 1
                self._guard_elapsed += 1
                self._quiet_run = self._quiet_run + 1 if rms < max(0.012, self._noise * 2.5) else 0
                if self._guard_elapsed >= self._guard_min and self._quiet_run >= 6:
                    self._guard_left = 0               # a cauda do áudio acabou: pode escutar
                else:
                    return
            cap = self._cap
            if cap is None or not cap.triggered:
                if rms < self._noise * 3:
                    self._noise += 0.02 * (rms - self._noise)
            if cap is None:
                cap = self._cap = self._new_capture()
            cap.feed(block)
            if self.streaming is not None and self.streaming.ready and cap.triggered:
                if not getattr(cap, "wake_stream_started", False):
                    audio = np.frombuffer(cap.carry()[0], dtype=np.int16).astype(np.float32) / 32768
                    cap.wake_stream_started = True
                else:
                    audio = block
                self.streaming.feed(audio, cap, self._epoch,
                                    lambda c, epoch: self._dispatch(self._stream_hit, c, epoch))
            # Examina o começo da fala sem esperar a frase inteira terminar.
            # O áudio completo continua na captura e será entregue ao comando.
            if (self.cfg.wake_low_latency and not (self.noisy and self.noisy())
                    and cap.triggered and not cap.done.is_set()
                    and self.cfg.wake_prefix_s > 0 and cap.silence == 0
                    and cap.voiced * 0.02 >= max(0.6, self.cfg.wake_prefix_s)
                    and getattr(cap, "wake_prefix_attempts", 0) < 3
                    and cap.voiced * .02 >= getattr(cap, "wake_prefix_next_s", 0)):
                job = self._submit(cap._snapshot(), max_inflight=1)
                if job is not None:
                    cap.wake_prefix_attempts = getattr(cap, "wake_prefix_attempts", 0) + 1
                    cap.wake_prefix_next_s = cap.voiced * .02 + .5
                    info = WakeInfo(voiced_s=cap.voiced * 0.02, partial=True,
                                    capture_id=cap.wake_capture_id,
                                    t_voice_start=cap.t_speech_start)
                    job.future.add_done_callback(
                        lambda fut, c=cap, j=job, i=info: self._dispatch(self._prefix_done, fut, c, j, i))
            if cap.done.is_set():
                self._cap = None
                self._finish(cap)

    def _stream_hit(self, cap: UtteranceCapture, epoch: int) -> None:
        with self._flock:
            if (epoch != self._epoch or self.muted or not self.ready or not self.cfg.wake_word_enabled
                    or (self.is_busy and self.is_busy()) or cap.voiced * 0.02 < self.MIN_VOICED_S):
                return
            self._prefix_capture = cap
            info = WakeInfo(partial=True, capture_id=cap.wake_capture_id,
                            voiced_s=cap.voiced * 0.02, t_voice_start=cap.t_speech_start)
        try:
            self._handle(Heard(self.cfg.wake_word), info)
        finally:
            with self._flock:
                if self._prefix_capture is cap:
                    self._prefix_capture = None

    def _prefix_done(self, fut: Future, cap: UtteranceCapture, job: _Job, info: WakeInfo) -> None:
        try:
            heard = fut.result()
        except Exception:
            log.debug("STT antecipado falhou; mantendo captura normal", exc_info=True)
            return
        with self._flock:
            if (self._cap is not cap or job.epoch != self._epoch or self.muted or not self.ready
                    or not self.cfg.wake_word_enabled
                    or (self.is_busy is not None and self.is_busy())):
                return
            match = match_wake(heard.text, self.cfg.wake_word)
            if not match.found:
                return
            # Nunca executa um comando parcial. take_carry entrega a fala completa.
            info.t_stt_start, info.t_stt_end = job.t_start, job.t_end
            self._prefix_capture = cap
        try:
            self._handle(heard, info)
        finally:
            with self._flock:
                if self._prefix_capture is cap:
                    self._prefix_capture = None

    def _new_capture(self) -> UtteranceCapture:
        c = self.cfg
        soft = c.wake_soft_silence_ms if (c.wake_soft_silence_ms > 0 and c.wake_low_latency) else 0
        # Não endureça o VAD quando toca música: o modo 3 também apagava
        # chamadas curtas. Nome, contexto e duração continuam filtrando ativações.
        cap = UtteranceCapture(aggressiveness=c.vad_aggressiveness, start_timeout_s=3600.0,
                                end_silence_ms=c.wake_end_silence_ms, max_s=c.wake_max_utterance_s,
                                soft_silence_ms=soft, on_soft_end=self._soft if soft else None,
                                on_resume=self._resume if soft else None, clock=self.audio_clock,
                                pre_ms=c.wake_pre_buffer_ms, post_ms=c.wake_post_buffer_ms,
                                min_rms=c.vad_min_rms, start_voiced_ms=80)
        cap.min_voiced_for_soft = 8
        self._capture_serial += 1
        cap.wake_capture_id = self._capture_serial
        return cap

    # ---- STT ---------------------------------------------------------------------------------------
    def _invalidate(self) -> None:
        if self._spec is not None:
            self._spec.valid = False
        self._spec = None

    def _soft(self, pcm: bytes) -> None:               # silêncio curto: já começa a transcrever (pode ser descartado)
        if self._signal_seconds(pcm) < self.MIN_VOICED_S:
            return  # a cauda do VAD após um estalo não conta como voz real
        if self.noisy and self.noisy() and len(pcm) / 2 / RATE > self.cfg.wake_music_max_voiced_s:
            return                                      # com música: frase longa será descartada, não gasta STT especulando
        job = self._submit(pcm, max_inflight=1)
        if job is not None:
            self._spec = job

    def _signal_seconds(self, pcm: bytes) -> float:
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
        count = len(samples) // (RATE // 50)
        if not count:
            return 0.0
        frames = samples[:count * (RATE // 50)].reshape(count, RATE // 50)
        energy = np.sqrt(np.mean(frames * frames, axis=1))
        threshold = max(self.cfg.vad_min_rms, self._noise * 1.7)
        return float(np.count_nonzero(energy >= threshold)) * 0.02

    def _resume(self) -> None:                          # a pessoa continuou falando: a especulativa não vale
        self._invalidate()

    def _submit(self, pcm: bytes, max_inflight: int = 2) -> _Job | None:
        with self._lock:
            if self._inflight >= max_inflight:
                return None
            self._inflight += 1
        job = _Job(len(pcm), None)
        job.epoch = self._epoch
        job.future = self._pool.submit(self._run, pcm, job)
        return job

    def _run(self, pcm: bytes, job: _Job) -> Heard:
        job.t_start = time.perf_counter()
        try:
            res = self.transcribe(pcm)
            self.stats["transcribed"] += 1
            return res if isinstance(res, Heard) else Heard(str(res or ""))
        finally:
            job.t_end = time.perf_counter()
            self.last_stt_s = job.t_end - job.t_start
            with self._lock:
                self._inflight -= 1

    def _finish(self, cap: UtteranceCapture) -> None:
        self._completed.append((cap.wake_capture_id, Carry(*cap.carry())))
        while len(self._completed) > 1 and sum(len(item[1].pcm) for item in self._completed) > self.CARRY_MAX_BYTES:
            self._completed.popleft()
        pcm, spec = cap.result, self._spec
        self._spec = None
        if pcm is None:
            return
        voiced_s = cap.voiced * 0.02
        if voiced_s < self.MIN_VOICED_S or self._signal_seconds(pcm) < self.MIN_VOICED_S:
            self.stats["dropped"] += 1
            return
        if self.noisy and self.noisy() and voiced_s > self.cfg.wake_music_max_voiced_s:
            self.stats["music_skipped"] += 1           # letra cantada/conversa longa com música tocando: nem tenta
            return
        self.stats["utterances"] += 1
        log.debug("[WAKE] Voz detectada (%.1fs)", len(pcm) / 2 / RATE)
        used_spec = spec is not None and spec.valid and spec.pcm_len == len(pcm)
        job = spec if used_spec else self._submit(pcm)
        if job is None:                                 # STT atrasado: melhor perder uma frase que acumular fila
            self.stats["dropped"] += 1
            log.debug("[WAKE] STT ocupado; frase descartada")
            return
        info = WakeInfo(voiced_s=voiced_s, spec_used=used_spec, t_voice_start=cap.t_speech_start,
                        capture_id=cap.wake_capture_id,
                        t_voice_end=cap.t_last_voice, t_vad_end=cap.t_end)
        job.future.add_done_callback(lambda fut, j=job, i=info: self._dispatch(self._on_done, fut, j, i))

    def _on_done(self, fut: Future, job: _Job, info: WakeInfo) -> None:
        if (not job.valid or job.epoch != self._epoch or self.muted or not self.ready
                or not self.cfg.wake_word_enabled or (self.is_busy and self.is_busy())):
            return
        try:
            heard = fut.result()
        except Exception:
            log.exception("Falha na transcrição da palavra de ativação")
            return
        info.t_stt_start, info.t_stt_end = job.t_start, job.t_end
        self._handle(heard, info)

    def _handle(self, heard: "Heard | str", info: WakeInfo | None = None) -> None:
        heard = heard if isinstance(heard, Heard) else Heard(str(heard))
        info = info or WakeInfo()
        if heard.no_speech >= 0.6:
            self.stats["low_conf"] += 1
            return
        m = match_wake(heard.text, self.cfg.wake_word)
        if self.on_heard is not None and heard.text:
            try:
                self.on_heard(heard.text, m.found)
            except Exception:
                log.exception("Diagnóstico de ativação falhou")
        if not m.found:
            log.debug("[WAKE] ouvido (sem ativação): %r", heard.text)
            return
        conf = wake_confidence(m, heard.confidence, info.voiced_s)
        if conf < self.cfg.wake_min_confidence:
            self.stats["low_conf"] += 1
            log.info("[WAKE] %r ignorado: confiança %.2f < %.2f", heard.text, conf, self.cfg.wake_min_confidence)
            return
        if m.command and info.capture_id is not None:
            with self._flock:
                later = any(identity > info.capture_id and piece.speech for identity, piece in self._completed)
                if self._cap is not None and self._cap.wake_capture_id > info.capture_id:
                    later = later or self._cap.carry()[1]
                if later:
                    # "Apolo, abra ... a calculadora": não executar só a
                    # primeira metade quando há fala posterior já capturada.
                    info.partial = True
        if info.partial:
            m = WakeMatch(True, "", m.token, m.score)
        now = self.clock()
        with self._lock:
            if now - self._last_fire < self.cfg.activation_cooldown_s:
                self.stats["debounced"] += 1
                log.info("[WAKE] ativação repetida ignorada (debounce)")
                return
            self._last_fire = now
        info.t_detect = time.perf_counter()
        info.text, info.command, info.token, info.confidence = heard.text, m.command, m.token, conf
        self.stats["activations"] += 1
        self.history.append(info)
        del self.history[:-100]
        log.info('[WAKE] "%s" detectado (confiança %.2f)%s', m.token or heard.text, conf,
                 f" + comando {m.command!r}" if m.command else "")
        if info.partial:
            log.info("[WAKE] Reconhecimento antecipado, antes do fim da frase; STT %.0f ms",
                     (info.stt_s or 0) * 1000)
        if info.latency_s is not None:
            log.info("[WAKE] Latência: %.0f ms (fim da fala → detecção: VAD %.0f ms + STT %.0f ms%s)",
                     info.latency_s * 1000, (info.vad_wait_s or 0) * 1000, (info.stt_s or 0) * 1000,
                     ", especulativo" if info.spec_used else "")
        try:
            with self._flock:
                self._handoff_id = info.capture_id
                self._handoff_partial = info.partial
            self.on_wake(m.command, heard.text, info)
        except Exception:
            log.exception("Falha ao ativar pelo wake word")
        finally:
            with self._flock:
                self._handoff_id = None
