"""Texto -> voz.

Motores (em ordem de preferência, com queda automática para o próximo se falhar):
  edge    vozes neurais da Microsoft via edge-tts. Gratuito, MUITO natural em português, exige internet.
  kokoro  Kokoro-82M local (ONNX). Gratuito/open-source, offline, ~80-300 MB, voz pt-BR masculina (pm_alex).
  sapi    voz do Windows (pyttsx3). Offline, mas robótica: último recurso.
O motor padrão é configurável (Configurações > Voz). Se o escolhido falhar, o APOLO continua falando com o próximo.

Pipeline: fila de textos -> thread de síntese (já prepara a frase N+1) -> pós-processamento -> thread de reprodução.
Todo áudio sai a 24 kHz mono, para manter UM stream de saída aberto durante a resposta inteira.

Pós-processamento (shape_audio): corta o silêncio do começo/fim de cada frase (MP3 costuma trazer silêncio inicial),
realça levemente os graves, normaliza o volume (mesmo nível em qualquer motor) e aplica fades curtos.
Entre frases entra uma pausa proporcional à pontuação (ponto > dois-pontos > vírgula).

Cache: frases curtas já sintetizadas voltam do disco (tts_cache.py), sem rede e sem síntese.
Tempos por resposta (self.timing): enqueue -> synth_start -> synth_end -> first_audio, abertura da saída de áudio,
motor, acerto de cache e esperas entre frases (stall_s). O log de latência usa isso para mostrar onde o tempo vai."""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from app.core.config import DATA_DIR, ROOT, Settings
from app.services.tts_cache import TTSCache

log = logging.getLogger("apolo.tts")
CHUNK = 1024
OUT_RATE = 24000
ENGINE_LABELS = {"edge": "NEURAL", "kokoro": "LOCAL", "sapi": "SAPI"}
KOKORO_MODELS = ("kokoro-v1.0.int8.onnx", "kokoro-v1.0.onnx")   # a quantizada (se existir) é mais rápida
KOKORO_VOICES = "voices-v1.0.bin"


# ---------------------------------------------------------------- áudio (puro numpy, testável)
def to_rate(x: np.ndarray, src: int, dst: int = OUT_RATE) -> np.ndarray:
    if src == dst or len(x) < 2:
        return x
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def shape_audio(x: np.ndarray, rate: int = OUT_RATE, bass_db: float = 0.0, target_rms: float = 0.11) -> np.ndarray:
    """float32 [-1, 1] -> float32 [-1, 1] com silêncio cortado, graves, volume normalizado e fades."""
    x = np.asarray(x, dtype=np.float32)
    if x.size < 64:
        return x
    # 1) corta silêncio inicial/final (janelas de 5 ms; limiar de ~-36 dB do pico)
    win = max(1, int(rate * 0.005))
    n = len(x) // win
    peak = float(np.max(np.abs(x)))
    if n >= 2 and peak > 1e-4:
        env = np.abs(x[:n * win]).reshape(n, win).max(axis=1)
        idx = np.nonzero(env > max(0.003, 0.015 * peak))[0]
        if idx.size:
            a = max(0, int(idx[0]) * win - int(rate * 0.02))
            b = min(len(x), (int(idx[-1]) + 1) * win + int(rate * 0.09))
            x = x[a:b]
    # 2) realce de graves (prateleira suave abaixo de ~220 Hz, fase zero via FFT)
    if bass_db > 0.05 and len(x) > 256:
        pad = 2048
        buf = np.concatenate([np.zeros(pad, np.float32), x, np.zeros(pad, np.float32)])
        spec = np.fft.rfft(buf)
        f = np.fft.rfftfreq(len(buf), 1.0 / rate)
        g = 10 ** (bass_db / 20.0)
        x = np.fft.irfft(spec * (1.0 + (g - 1.0) / (1.0 + (f / 220.0) ** 2)), len(buf))[pad:pad + len(x)].astype(np.float32)
    # 3) volume: leva o RMS das partes ativas ao alvo (limitado) e evita estourar
    p = float(np.max(np.abs(x)))
    if p > 1e-4:
        active = x[np.abs(x) > 0.05 * p]
        rms = float(np.sqrt(np.mean(active * active))) if active.size else p
        x = x * float(np.clip(target_rms / max(rms, 1e-4), 0.25, 4.0))
        p = float(np.max(np.abs(x)))
        if p > 0.92:
            x = x * (0.92 / p)
    # 4) fades de 6 ms (sem estalos entre frases)
    k = min(int(rate * 0.006), len(x) // 2)
    if k > 1:
        ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)
        x = x.copy()
        x[:k] *= ramp
        x[-k:] *= ramp[::-1]
    return x


def gap_for(text: str, base_ms: int) -> int:
    """Pausa depois desta frase, conforme a pontuação final."""
    t = text.rstrip()
    if not t:
        return 0
    c = t[-1]
    if c in ".!?…":
        return int(base_ms)
    if c in ";:":
        return int(base_ms * 0.7)
    if c == ",":
        return int(base_ms * 0.45)
    return int(base_ms * 0.6)


class TTSService:
    def __init__(self, cfg: Settings, on_level: Callable[[float], None] | None = None,
                 on_engine: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.on_level = on_level
        self.on_engine = on_engine                       # recebe o rótulo do motor em uso (NEURAL/LOCAL/SAPI)
        self.on_first_audio: Callable[[float], None] | None = None   # recebe time.perf_counter() do 1º som
        self.last_error = ""
        self.last_synth_s = 0.0
        self.engine = ""                                 # motor da última frase
        self.timing: dict = {}                           # tempos da resposta atual/última (ver docstring)
        self._cur_rt: dict | None = None                 # tempos da frase em síntese (só a thread de síntese escreve)
        self.cache = TTSCache(DATA_DIR / "tts_cache")
        self._text_q: queue.Queue = queue.Queue()
        self._audio_q: queue.Queue = queue.Queue()
        self._pending = 0
        self._cv = threading.Condition()
        self._gen = 0                                    # sobe a cada stop(): descarta trabalho antigo
        self._off_until = {"edge": 0.0}
        self._started = False
        self._resp_first = False
        self._sapi = None
        self._kokoro = None
        self._kokoro_lock = threading.Lock()
        self._kokoro_checked = 0.0
        self._kokoro_dead = False                        # biblioteca ausente ou falha de carga: não insiste

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._synth_loop, daemon=True, name="tts-synth").start()
        threading.Thread(target=self._play_loop, daemon=True, name="tts-play").start()
        if self.cfg.tts_engine == "kokoro":              # carrega o modelo uma vez, em segundo plano
            threading.Thread(target=self._load_kokoro, daemon=True, name="tts-warm").start()

    def label(self) -> str:
        return ENGINE_LABELS.get(self.engine or self.cfg.tts_engine, "NEURAL")

    @property
    def busy(self) -> bool:
        """True enquanto há fala na fila/tocando. O detector de wake word ignora o microfone nesse período."""
        return self._pending > 0

    def engines(self) -> list[str]:
        """Motor usado para gerar ARQUIVOS (áudios de ativação): somente o configurado em Configurações > Voz, sem queda
        automática para outro — uma voz diferente da escolhida soaria errada. SAPI só fala, não sintetiza."""
        return [] if self.cfg.tts_engine == "sapi" else [self.cfg.tts_engine]

    def voice_signature(self, engine: str) -> str:
        """Identifica motor + voz + ritmo + tom + graves. Usado para saber se os áudios pré-renderizados ficaram velhos."""
        return self._sig(engine)

    def play_pcm(self, pcm: np.ndarray, gap_ms: int = 0) -> None:
        """Toca um áudio JÁ PRONTO (int16 mono, OUT_RATE) pelo mesmo pipeline de saída da fala: sem síntese, sem rede.
        Participa de wait_idle()/busy/níveis do HUD como qualquer outra fala."""
        if pcm is None or len(pcm) == 0:
            return
        self.start()
        with self._cv:
            new_response = self._pending <= 0
            self._pending += 1
            gen = self._gen
            if new_response:
                self.timing = {"enqueue": time.perf_counter(), "stall_s": 0.0, "engine": "pre-rendered",
                               "cache_hit": True}
            rt = self.timing
        if new_response:
            self._resp_first = True
        self._audio_q.put(("pcm", np.asarray(pcm, dtype=np.int16), int(gap_ms), rt, gen))

    # ---- API pública -----------------------------------------------------
    def speak(self, text: str) -> None:
        if not text.strip():
            return
        self.start()
        with self._cv:
            new_response = self._pending <= 0
            self._pending += 1
            gen = self._gen
            if new_response:
                self.timing = {"enqueue": time.perf_counter(), "stall_s": 0.0}
            rt = self.timing
        if new_response:
            self._resp_first = True
            self._audio_q.put(("warm", rt, gen))         # abre o stream de saída enquanto a 1ª frase é sintetizada
        self._text_q.put((gen, text, rt))

    def wait_idle(self, timeout: float | None = None) -> None:
        with self._cv:
            self._cv.wait_for(lambda: self._pending <= 0, timeout)

    def prepare_output(self) -> None:
        """Abre a saída enquanto a resposta é gerada, antes da primeira frase."""
        self.start()
        self._audio_q.put(("warm", {}, self._gen))

    def stop(self) -> None:
        with self._cv:
            self._gen += 1
            self._pending = 0
            self._cv.notify_all()
        for q in (self._text_q, self._audio_q):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass

    def _sig(self, engine: str) -> str:
        c = self.cfg
        if engine == "edge":
            return f"edge|{c.tts_voice}|{c.tts_rate}|{c.tts_pitch_hz}|{c.tts_bass_db}|v2"
        return f"kokoro|{c.kokoro_voice}|{c.kokoro_lang}|{c.tts_rate}|{c.kokoro_pitch_st}|{c.tts_bass_db}|v2"

    def prerender(self, phrases: list[str], busy: Callable[[], bool] | None = None, delay_s: float = 20.0) -> None:
        """Sintetiza em segundo plano frases fixas e as guarda no cache (só no motor preferido, só se faltarem)."""
        if not self.cfg.tts_cache or self.cfg.tts_engine == "sapi":
            return

        def work():
            time.sleep(delay_s)
            loop = asyncio.new_event_loop()
            engine = self.cfg.tts_engine
            done = 0
            try:
                for text in phrases:
                    if self._pending > 0 or (busy and busy()):
                        time.sleep(3.0)                  # há uma conversa em andamento: não compete por rede/CPU
                    if self.cache.has(self._sig(engine), text):
                        continue
                    try:
                        x = self._synth_with(engine, loop, text)
                    except Exception as e:
                        log.info("Pré-renderização interrompida (%r)", e)
                        break
                    self.cache.put(self._sig(engine), text, np.clip(x * 32767.0, -32768, 32767).astype(np.int16))
                    done += 1
                    time.sleep(0.4)
            finally:
                loop.close()
            if done:
                log.info("Pré-renderizei %d frases fixas no cache de voz", done)

        threading.Thread(target=work, daemon=True, name="tts-prerender").start()

    def synthesize(self, text: str, engine: str) -> tuple[np.ndarray, int, float]:
        """Síntese síncrona de UM motor, sem tocar (para testes e comparação). Devolve (pcm int16, taxa, segundos).
        Levanta exceção se o motor falhar."""
        loop = asyncio.new_event_loop()
        try:
            t0 = time.perf_counter()
            x = self._synth_with(engine, loop, text)
            return (np.clip(x * 32767.0, -32768, 32767).astype(np.int16), OUT_RATE, time.perf_counter() - t0)
        finally:
            loop.close()

    def _done(self, gen: int) -> None:
        with self._cv:
            if gen == self._gen:
                self._pending = max(0, self._pending - 1)
            self._cv.notify_all()

    def _level(self, v: float) -> None:
        if self.on_level:
            try:
                self.on_level(v)
            except Exception:
                pass

    def _set_engine(self, name: str) -> None:
        if name != self.engine:
            self.engine = name
            if self.on_engine:
                try:
                    self.on_engine(ENGINE_LABELS.get(name, name.upper()))
                except Exception:
                    pass

    # ---- síntese -----------------------------------------------------------
    def _order(self) -> list[str]:
        pref = self.cfg.tts_engine
        if pref == "sapi":
            return ["sapi"]
        return ["kokoro", "edge", "sapi"] if pref == "kokoro" else ["edge", "kokoro", "sapi"]

    def _synth_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            gen, text, rt = self._text_q.get()
            if gen != self._gen:
                continue
            rt.setdefault("synth_start", time.perf_counter())
            self._cur_rt = rt
            try:
                item = self._synthesize_item(loop, text, rt)
            except Exception:
                log.exception("Falha inesperada na síntese")
                item = ("sapi", text, 0)
            rt.setdefault("synth_end", time.perf_counter())
            if gen == self._gen:
                self._audio_q.put(item + (rt, gen))

    def _synthesize_item(self, loop, text: str, rt: dict | None = None):
        rt = rt if rt is not None else {}
        gap = gap_for(text, self.cfg.tts_pause_ms)
        use_cache = bool(self.cfg.tts_cache)
        for name in self._order():
            if name == "sapi":
                self._set_engine("sapi")
                rt.setdefault("engine", "sapi")
                return ("sapi", text, gap)
            if use_cache and name != "sapi":
                hit = self.cache.get(self._sig(name), text)
                if hit is not None:
                    self._set_engine(name)
                    rt.setdefault("engine", name)
                    rt.setdefault("cache_hit", True)
                    return ("pcm", hit, gap)
            if time.time() < self._off_until.get(name, 0.0):
                continue
            t0 = time.perf_counter()
            try:
                x = self._synth_with(name, loop, text)
            except _Unavailable as e:
                self.last_error = f"{name}: {e}"
                continue
            except Exception as e:
                self.last_error = f"{name}: {e}"
                log.warning("%s falhou (%r); usando o próximo motor por 2 min", name, e)
                self._off_until[name] = time.time() + 120
                continue
            self.last_synth_s = time.perf_counter() - t0
            log.debug("TTS %s: %.2fs para %d caracteres", name, self.last_synth_s, len(text))
            self._set_engine(name)
            rt.setdefault("engine", name)
            rt.setdefault("cache_hit", False)
            pcm = np.clip(x * 32767.0, -32768, 32767).astype(np.int16)
            if use_cache:
                self.cache.put(self._sig(name), text, pcm)
            return ("pcm", pcm, gap)
        self._set_engine("sapi")
        rt.setdefault("engine", "sapi")
        return ("sapi", text, gap)

    def _synth_with(self, name: str, loop, text: str) -> np.ndarray:
        if name == "edge":
            raw, rate = loop.run_until_complete(asyncio.wait_for(self._edge(text), 12))
        elif name == "kokoro":
            raw, rate = self._kokoro_synth(text)
        else:
            raise _Unavailable("motor desconhecido")
        t0 = time.perf_counter()
        x = to_rate(np.asarray(raw, dtype=np.float32), rate, OUT_RATE)
        out = shape_audio(x, OUT_RATE, self.cfg.tts_bass_db)
        if self._cur_rt is not None and "post_s" not in self._cur_rt:
            self._cur_rt["post_s"] = time.perf_counter() - t0
        return out

    # -- edge-tts
    async def _edge(self, text: str):
        try:
            import edge_tts
            import miniaudio
        except Exception as e:
            raise _Unavailable(f"biblioteca ausente ({e})") from e
        rate = max(-50, min(50, int(self.cfg.tts_rate)))
        pitch = max(-50, min(50, int(self.cfg.tts_pitch_hz)))
        try:
            comm = edge_tts.Communicate(text, self.cfg.tts_voice, rate=f"{rate:+d}%", pitch=f"{pitch:+d}Hz")
        except (ValueError, TypeError) as e:       # versão do edge-tts sem/estrita com pitch
            log.warning("edge-tts recusou o pitch (%r); usando só a velocidade", e)
            comm = edge_tts.Communicate(text, self.cfg.tts_voice, rate=f"{rate:+d}%")
        rt = self._cur_rt if self._cur_rt is not None else {}
        first = first_chunk = t_start = time.perf_counter()
        mp3 = bytearray()
        async for ch in comm.stream():
            if ch["type"] == "audio":
                if not mp3:
                    first_chunk = time.perf_counter()
                mp3 += ch["data"]
        t_stream = time.perf_counter()
        if not mp3:
            raise RuntimeError("edge-tts não devolveu áudio")
        dec = miniaudio.decode(bytes(mp3), output_format=miniaudio.SampleFormat.SIGNED16,
                               nchannels=1, sample_rate=OUT_RATE)
        if "edge_first_chunk_s" not in rt:               # só a 1ª frase da resposta interessa ao log
            rt["edge_first_chunk_s"] = first_chunk - t_start      # conexão (TLS/WebSocket) + 1º pedaço de áudio
            rt["edge_stream_s"] = t_stream - first_chunk          # o resto do áudio chegando
            rt["edge_decode_s"] = time.perf_counter() - t_stream  # decodificar o MP3
            rt["edge_audio_s"] = len(dec.samples) / OUT_RATE
        return np.frombuffer(dec.samples, dtype=np.int16).astype(np.float32) / 32768.0, OUT_RATE

    # -- Kokoro (local)
    def kokoro_paths(self) -> tuple[str, str] | None:
        c = self.cfg
        dirs = [Path(c.kokoro_model_dir)] if c.kokoro_model_dir else [ROOT / "models" / "kokoro",
                                                                      DATA_DIR / "models" / "kokoro"]
        for d in dirs:
            voices = d / KOKORO_VOICES
            for m in KOKORO_MODELS:
                if (d / m).is_file() and voices.is_file():
                    return str(d / m), str(voices)
        return None

    def _load_kokoro(self) -> bool:
        with self._kokoro_lock:
            if self._kokoro is not None:
                return True
            if self._kokoro_dead or time.time() - self._kokoro_checked < 20:
                return False
            self._kokoro_checked = time.time()
            paths = self.kokoro_paths()
            if not paths:
                self.last_error = "kokoro: arquivos do modelo não encontrados (veja o README: scripts/download_voice.py)"
                return False
            try:
                from kokoro_onnx import Kokoro
            except Exception as e:
                self._kokoro_dead = True
                self.last_error = f"kokoro: biblioteca não instalada ({e})"
                log.info(self.last_error)
                return False
            try:
                t0 = time.perf_counter()
                k = Kokoro(*paths)
                k.create("Olá.", voice=self.cfg.kokoro_voice, speed=1.0, lang=self.cfg.kokoro_lang)  # aquece o ONNX
                self._kokoro = k
                log.info("Kokoro carregado e aquecido em %.1fs (%s)", time.perf_counter() - t0, Path(paths[0]).name)
                return True
            except Exception as e:
                self._kokoro_dead = True
                self.last_error = f"kokoro: falha ao carregar ({e})"
                log.exception("Falha ao carregar o Kokoro")
                return False

    def _kokoro_synth(self, text: str):
        if not self._load_kokoro():
            raise _Unavailable(self.last_error or "não carregado")
        c = self.cfg
        ratio = 2.0 ** (float(c.kokoro_pitch_st) / 12.0)       # <1 = mais grave
        speed = max(0.5, min(1.6, 1.0 + int(c.tts_rate) / 100.0))
        # gera mais rápido e toca na taxa reduzida: o tom desce, o tempo final fica o mesmo
        samples, sr = self._kokoro.create(text, voice=c.kokoro_voice, speed=speed / ratio, lang=c.kokoro_lang)
        return np.asarray(samples, dtype=np.float32), int(round(sr * ratio))

    # ---- reprodução --------------------------------------------------------
    def _play_loop(self) -> None:
        stream = None
        prev_end, prev_gap, last_rt = 0.0, 0, None
        idle_since = time.monotonic()
        output_device = self.cfg.output_device
        while True:
            try:
                item = self._audio_q.get(timeout=0.5)
            except queue.Empty:
                if stream is not None and time.monotonic() - idle_since >= 3.0:
                    stream = self._close(stream)
                continue
            idle_since = time.monotonic()
            if output_device != self.cfg.output_device:
                stream = self._close(stream)
                output_device = self.cfg.output_device
            kind, gen = item[0], item[-1]
            if gen != self._gen:
                continue
            if kind == "warm":
                if stream is None:
                    t0 = time.perf_counter()
                    try:
                        stream = self._open(OUT_RATE)
                        item[1]["stream_open_s"] = time.perf_counter() - t0
                    except Exception as e:
                        self.last_error = str(e)
                        log.warning("Não consegui pré-abrir a saída de áudio: %r", e)
                continue
            rt = item[-2]
            try:
                if kind == "pcm":
                    pcm, gap_ms = item[1], item[2]
                    if stream is None:
                        t0 = time.perf_counter()
                        stream = self._open(OUT_RATE)
                        rt["stream_open_s"] = time.perf_counter() - t0
                    if prev_gap or last_rt is rt:
                        idle = time.perf_counter() - prev_end
                        if last_rt is rt and idle > prev_gap / 1000.0:
                            rt["stall_s"] = rt.get("stall_s", 0.0) + (idle - prev_gap / 1000.0)   # frase seguinte atrasou
                        wait = prev_gap / 1000.0 - idle
                        if wait > 0.01:
                            stream.write(np.zeros((int(wait * OUT_RATE), 1), dtype=np.int16))
                    self._first_audio(rt)
                    for i in range(0, len(pcm), CHUNK):
                        if gen != self._gen:
                            break
                        chunk = pcm[i:i + CHUNK]
                        stream.write(chunk.reshape(-1, 1))
                        rms = float(np.sqrt(np.mean((chunk.astype(np.float32) / 32768.0) ** 2)))
                        self._level(min(1.0, rms * 5))
                    prev_end, prev_gap, last_rt = time.perf_counter(), gap_ms, rt
                else:
                    stream = self._close(stream)
                    self._first_audio(rt)
                    self._speak_sapi(item[1])
                    prev_gap, last_rt = 0, None
            except Exception as e:
                self.last_error = str(e)
                log.exception("Falha ao reproduzir áudio")
                stream = self._close(stream)
                prev_gap, last_rt = 0, None
            finally:
                self._done(gen)
                idle_since = time.monotonic()
                if self._pending <= 0:
                    self._level(0.0)
                    prev_gap, last_rt = 0, None

    def _first_audio(self, rt: dict | None = None) -> None:
        if self._resp_first:
            self._resp_first = False
            now = time.perf_counter()
            if rt is not None:
                rt.setdefault("first_audio", now)
            cb = self.on_first_audio
            if cb:
                try:
                    cb(now)
                except Exception:
                    pass

    def _open(self, rate: int):
        import sounddevice as sd
        s = sd.OutputStream(samplerate=rate, channels=1, dtype="int16", device=self.cfg.output_device)
        s.start()
        return s

    @staticmethod
    def _close(stream):
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        return None

    def _speak_sapi(self, text: str) -> None:
        """Plano B offline (voz do Windows). Roda sempre nesta mesma thread (exigência do COM)."""
        import random
        try:
            if self._sapi is None:
                try:
                    import pythoncom
                    pythoncom.CoInitialize()
                except Exception:
                    pass
                import pyttsx3
                eng = pyttsx3.init()
                for v in eng.getProperty("voices"):
                    if any(k in (v.name or "").lower() for k in ("portuguese", "brazil", "maria", "daniel")):
                        eng.setProperty("voice", v.id)
                        break
                eng.connect("started-word", lambda *a, **k: self._level(random.uniform(0.35, 0.8)))
                self._sapi = eng
            self._sapi.setProperty("rate", int(190 + self.cfg.tts_rate * 1.5))
            self._sapi.say(text)
            self._sapi.runAndWait()
        except Exception as e:
            self.last_error = f"sapi: {e}"
            log.exception("Voz do Windows falhou")
            self._sapi = None


class _Unavailable(Exception):
    """Motor não disponível agora (sem biblioteca/modelo): pula para o próximo sem castigo de 2 min."""
