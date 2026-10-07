"""Reprodução de música LOCAL (arquivos MP3/FLAC/OGG/WAV da sua pasta) + teclas de mídia do Windows.

LocalPlayer
  - decodifica o arquivo com miniaudio (já é dependência do projeto, usada pelo TTS) numa thread, já para
    44,1 kHz estéreo int16, e toca com sounddevice (OutputStream) — a mesma biblioteca do microfone;
  - fila de faixas, próxima/anterior, pausa/continua, volume (ganho em software com rampa, sem estalos),
    "ducking" (baixa o volume enquanto o APOLO ouve/fala) e pré-decodificação da próxima faixa (troca instantânea);
  - nada de rede, DRM ou serviço externo: só arquivos que você já tem.
  Limite honesto: a faixa inteira é decodificada em RAM (~42 MB para 4 min); formatos: mp3, flac, ogg (vorbis), wav
  (m4a/aac NÃO são suportados pelo miniaudio).

MediaKeys
  - envia as teclas de mídia do Windows (play/pause, próxima, anterior, parar, volume ±). Controla o player que o
    sistema considerar ativo (Spotify, YouTube no navegador...). É o mesmo que apertar as teclas do teclado.

Os dois aceitam dublês (decoder/stream_factory/sender) para teste sem placa de som."""
from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger("apolo.music.player")

PLAY_RATE = 44100
SUPPORTED_EXT = (".mp3", ".flac", ".ogg", ".wav")


@dataclass
class LocalTrack:
    path: Path
    title: str
    artist: str = ""
    album: str = ""


# ------------------------------------------------------------------------------------------- decodificação
def default_decoder(path: Path) -> np.ndarray:
    """Arquivo -> int16 estéreo (n, 2) a 44,1 kHz."""
    try:
        import miniaudio
    except ImportError:
        miniaudio = None
    if miniaudio is not None:
        d = miniaudio.decode_file(str(path), output_format=miniaudio.SampleFormat.SIGNED16, nchannels=2,
                                  sample_rate=PLAY_RATE)
        return np.frombuffer(d.samples, dtype=np.int16).reshape(-1, 2).copy()
    if Path(path).suffix.lower() == ".wav":                       # sem miniaudio, ao menos WAV 16 bits
        import wave
        with wave.open(str(path), "rb") as w:
            ch, rate, sw = w.getnchannels(), w.getframerate(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if sw != 2:
            raise RuntimeError("WAV de 16 bits apenas")
        x = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch)
        x = np.repeat(x, 2, axis=1) if ch == 1 else x[:, :2]
        if rate != PLAY_RATE:
            idx = np.linspace(0, len(x) - 1, int(len(x) * PLAY_RATE / rate))
            x = np.stack([np.interp(idx, np.arange(len(x)), x[:, c]) for c in (0, 1)], axis=1).astype(np.int16)
        return x
    raise RuntimeError("Para tocar MP3/FLAC/OGG instale o miniaudio (pip install miniaudio).")


def default_stream_factory(callback, device):
    import sounddevice as sd
    return sd.OutputStream(samplerate=PLAY_RATE, channels=2, dtype="int16", device=device, callback=callback,
                           blocksize=1024)


# ------------------------------------------------------------------------------------------- player
class LocalPlayer:
    def __init__(self, decoder: Callable[[Path], np.ndarray] | None = None, stream_factory=None,
                 output_device: int | None = None, on_change: Callable[[], None] | None = None,
                 volume: int = 70, duck_pct: int = 25):
        self.decoder = decoder or default_decoder
        self.stream_factory = stream_factory or default_stream_factory
        self.output_device = output_device
        self.on_change = on_change
        self.queue: list[LocalTrack] = []
        self.index = -1
        self.volume = max(0, min(100, volume))
        self.duck_pct = duck_pct
        self._ducked = False
        self.is_playing = False
        self.error = ""
        self._samples: np.ndarray | None = None
        self._pos = 0
        self._gain_cur = 0.0
        self._stream = None
        self._gen = 0                              # invalida decodificações antigas
        self._lock = threading.RLock()
        self._cache: dict[int, np.ndarray] = {}    # pré-decodificação da próxima faixa
        self._advancing = False
        self._loading = False                      # há uma faixa sendo decodificada (pausa/continua nesse intervalo valem)

    # ---- estado ------------------------------------------------------------------------------------
    @property
    def current(self) -> LocalTrack | None:
        return self.queue[self.index] if 0 <= self.index < len(self.queue) else None

    @property
    def position_s(self) -> float:
        return self._pos / PLAY_RATE

    def _gain_target(self) -> float:
        pct = self.volume * (self.duck_pct / 100.0 if self._ducked else 1.0)
        return (max(0.0, min(100.0, pct)) / 100.0) ** 2          # curva perceptual simples

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.exception("on_change falhou")

    # ---- controle ----------------------------------------------------------------------------------
    def play_queue(self, tracks: list[LocalTrack], start: int = 0) -> None:
        with self._lock:
            self.queue, self.index = list(tracks), start - 1
        self._advance(+1, user=True)

    def _advance(self, step: int, user: bool = False) -> bool:
        with self._lock:
            idx = self.index + step
            if not 0 <= idx < len(self.queue):
                if not user:                                   # fim da fila: para sozinho
                    self.is_playing = False
                    self._samples, self._pos = None, 0
                    self._close_stream()
                    self._notify()
                return False
            self.index = idx
            self._gen += 1
            gen = self._gen
            self.is_playing = True
            self._advancing = False
            self._loading = True
        threading.Thread(target=self._load, args=(idx, gen), daemon=True, name="music-decode").start()
        return True

    def _decode(self, idx: int) -> np.ndarray:
        with self._lock:
            hit = self._cache.pop(idx, None)
        return hit if hit is not None else self.decoder(self.queue[idx].path)

    def _load(self, idx: int, gen: int) -> None:
        try:
            samples = self._decode(idx)
        except Exception as e:
            log.warning("Não consegui decodificar %s: %s", self.queue[idx].path.name, e)
            self.error = str(e)
            if gen == self._gen:
                self._advance(+1)                              # pula a faixa ruim
            return
        with self._lock:
            if gen != self._gen:
                return
            self._loading = False
            self._samples, self._pos, self._gain_cur = samples, 0, 0.0   # is_playing fica como a pessoa deixou (pausou? continua pausado)
            self._open_stream()
        self._notify()
        self._prefetch(idx + 1, gen)

    def _prefetch(self, idx: int, gen: int) -> None:
        if not 0 <= idx < len(self.queue) or idx in self._cache:
            return
        try:
            data = self.decoder(self.queue[idx].path)
        except Exception:
            return
        with self._lock:
            if gen == self._gen:
                self._cache = {idx: data}

    def _open_stream(self) -> None:
        if self._stream is not None:
            return
        try:
            self._stream = self.stream_factory(self._callback, self.output_device)
            self._stream.start()
        except Exception as e:
            log.warning("Não consegui abrir a saída de áudio: %s", e)
            self.error, self._stream, self.is_playing = str(e), None, False

    def _close_stream(self) -> None:
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass

    def pause(self) -> None:
        with self._lock:
            self.is_playing = False
        self._notify()

    def resume(self) -> None:
        with self._lock:
            if self._samples is not None or self._loading:    # "continue" durante a decodificação também vale
                self.is_playing = True
        self._notify()

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            self.is_playing, self._samples, self._pos, self._loading = False, None, 0, False
            self._cache.clear()
        self._close_stream()
        self._notify()

    def next(self) -> bool:
        return self._advance(+1, user=True)

    def previous(self) -> bool:
        if self._pos > 3 * PLAY_RATE:                          # como nos players: depois de 3 s, "anterior" reinicia a faixa
            with self._lock:
                self._pos = 0
            return True
        return self._advance(-1, user=True)

    def set_volume(self, pct: int) -> int:
        self.volume = max(0, min(100, int(pct)))
        self._notify()
        return self.volume

    def duck(self, on: bool) -> None:
        self._ducked = bool(on)

    def close(self) -> None:
        self.stop()

    # ---- thread de áudio ----------------------------------------------------------------------------
    def _callback(self, outdata, frames, time_info, status) -> None:
        samples = self._samples
        target = self._gain_target() if self.is_playing else 0.0
        if samples is None or (not self.is_playing and self._gain_cur < 1e-4):
            outdata.fill(0)
            self._gain_cur = 0.0 if not self.is_playing else self._gain_cur
            return
        pos = self._pos
        chunk = samples[pos:pos + frames]
        n = len(chunk)
        ramp = np.linspace(self._gain_cur, target, n, dtype=np.float32) if n else np.zeros(0, np.float32)
        outdata[:n] = (chunk.astype(np.float32) * ramp[:, None]).astype(np.int16)
        if n < frames:
            outdata[n:] = 0
        self._gain_cur = target
        if self.is_playing:
            self._pos = pos + n
            if self._pos >= len(samples) and not self._advancing:    # fim da faixa: próxima, fora da thread de áudio
                self._advancing = True
                threading.Thread(target=self._advance, args=(+1,), daemon=True, name="music-next").start()


# ------------------------------------------------------------------------------------------- teclas de mídia
class MediaKeys:
    """Teclas de mídia virtuais do Windows (VK_MEDIA_*). No Windows apenas; em outros sistemas devolve False."""

    CODES = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
             "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD}

    def __init__(self, sender: Callable[[int], None] | None = None):
        self._sender = sender

    @property
    def available(self) -> bool:
        return self._sender is not None or sys.platform == "win32"

    def press(self, key: str, times: int = 1) -> bool:
        code = self.CODES[key]
        if not self.available:
            return False
        for _ in range(max(1, times)):
            (self._sender or self._win_press)(code)
            time.sleep(0.03)
        return True

    @staticmethod
    def _win_press(code: int) -> None:
        import ctypes
        KEYEVENTF_KEYUP, KEYEVENTF_EXTENDEDKEY = 0x0002, 0x0001
        user32 = ctypes.windll.user32
        user32.keybd_event(code, 0, KEYEVENTF_EXTENDEDKEY, 0)
        user32.keybd_event(code, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
