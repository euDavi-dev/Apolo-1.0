"""Captura contínua do microfone (um único InputStream compartilhado).

O callback do PortAudio só copia o bloco para uma fila; uma thread consumidora entrega
cada bloco ao "sink" atual (detector de palmas OU gravador de fala OU calibrador).
Um watchdog reabre o microfone se ele for desconectado ou travar.

Troca de sink SEM PERDER ÁUDIO: quando a ativação por voz dispara, a pessoa pode já estar falando o comando. O motor
guarda (backlog, até 3 s) os blocos que chegam sem sink e os entrega, em ordem, ao próximo gravador
(use_sink(..., backlog=True)). A troca é atômica: um bloco ou vai para o sink antigo, ou para o backlog; nunca some."""
from __future__ import annotations

import logging
import queue
import threading
import time
from contextlib import contextmanager
from typing import Callable

import numpy as np

try:
    import sounddevice as sd
except Exception:  # sem PortAudio: o app abre, mas avisa que não há microfone
    sd = None

log = logging.getLogger("apolo.audio")

RATE = 16000
BLOCK_MS = 20
BLOCK = RATE * BLOCK_MS // 1000  # 320 amostras
BACKLOG_MAX = 150                # 3 s de blocos de 20 ms


def _devices(kind: str) -> list[tuple[int, str]]:
    """Dispositivos da API de áudio padrão (evita duplicatas MME/WASAPI/DirectSound)."""
    if sd is None:
        return []
    try:
        host = sd.query_hostapis(sd.default.hostapi)
        key = "max_input_channels" if kind == "input" else "max_output_channels"
        out = []
        for i in host["devices"]:
            d = sd.query_devices(i)
            if d[key] > 0:
                out.append((i, d["name"]))
        return out
    except Exception:
        log.exception("Falha ao listar dispositivos")
        return []


def list_input_devices() -> list[tuple[int, str]]:
    return _devices("input")


def list_output_devices() -> list[tuple[int, str]]:
    return _devices("output")


def friendly_mic_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "no default input" in msg or "no input" in msg or "invalid device" in msg or isinstance(exc, ValueError):
        return "Microfone não encontrado. Conecte um microfone ou escolha outro em Configurações."
    return ("Não consegui acessar o microfone. Verifique se o Windows permite o acesso em "
            "Configurações > Privacidade > Microfone.")


class AudioEngine:
    def __init__(self, device: int | None = None,
                 on_level: Callable[[float], None] | None = None,
                 on_status: Callable[[bool, str], None] | None = None):
        self.device = device
        self.on_level = on_level
        self.on_status = on_status
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._sink: Callable[[np.ndarray], None] | None = None
        self._sink_lock = threading.RLock()      # RLock: um sink pode ativar o APOLO, que troca o sink na mesma thread
        self._keep_backlog = False
        self._backlog: list[np.ndarray] = []
        self._stream = None
        self._lock = threading.RLock()
        self._running = False
        self._gen = 0
        self._last_block = 0.0
        # Diagnóstico de latência: cada bloco leva o instante em que o PortAudio o entregou. Medir pelo
        # momento do PROCESSAMENTO esconderia atrasos (fila acumulada) e faria o "fim da fala" parecer tardio.
        self.block_ts = 0.0       # chegada do bloco que está sendo processado agora
        self.lag_s = 0.0          # atraso entre a chegada e o processamento desse bloco
        self.dropped = 0          # blocos descartados porque a fila encheu (consumidor travado)

    # ---- ciclo de vida -------------------------------------------------
    @property
    def ok(self) -> bool:
        s = self._stream
        return bool(s is not None and s.active)

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return self.ok
            self._running = True
            self._gen += 1
            self._last_block = time.monotonic()
            opened = self._open()
            for target in (self._consume, self._watch):
                threading.Thread(target=target, args=(self._gen,), daemon=True, name=f"audio-{target.__name__}").start()
            return opened

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._gen += 1
            self._close()

    def restart(self) -> bool:
        self.stop()
        return self.start()

    def _notify(self, ok: bool, msg: str = "") -> None:
        if self.on_status:
            try:
                self.on_status(ok, msg)
            except Exception:
                log.exception("on_status falhou")

    def _close(self) -> None:
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass

    def _make_stream(self, rate: int, resample: bool) -> None:
        bs = int(rate * BLOCK_MS / 1000)
        cb = self._cb_resample if resample else self._cb
        self._stream = sd.InputStream(device=self.device, channels=1, samplerate=rate,
                                      blocksize=bs, dtype="float32", callback=cb)
        self._stream.start()

    def _open(self) -> bool:
        with self._lock:
            self._close()
            if sd is None:
                self._notify(False, "Biblioteca de áudio (sounddevice/PortAudio) indisponível.")
                return False
            try:
                try:
                    self._make_stream(RATE, resample=False)
                except sd.PortAudioError:
                    native = int(sd.query_devices(self.device, "input")["default_samplerate"])
                    log.info("Microfone não aceita 16 kHz; usando %s Hz com reamostragem", native)
                    self._close()
                    self._make_stream(native, resample=True)
            except Exception as e:
                log.warning("Não foi possível abrir o microfone: %s", e)
                self._close()
                self._notify(False, friendly_mic_error(e))
                return False
            self._last_block = time.monotonic()
            self._notify(True, "")
            return True

    # ---- callbacks / threads ------------------------------------------
    def _cb(self, indata, frames, t, status):
        ts = time.perf_counter()
        try:
            self._q.put_nowait((ts, indata[:, 0].copy()))
        except queue.Full:
            self.dropped += 1

    def _cb_resample(self, indata, frames, t, status):
        x = indata[:, 0]
        y = np.interp(np.linspace(0, len(x) - 1, BLOCK), np.arange(len(x)), x).astype(np.float32)
        ts = time.perf_counter()
        try:
            self._q.put_nowait((ts, y))
        except queue.Full:
            self.dropped += 1

    def _consume(self, gen: int) -> None:
        last_emit = 0.0
        while self._running and gen == self._gen:
            try:
                ts, blk = self._q.get(timeout=0.3)
            except queue.Empty:
                continue
            self.block_ts = ts
            self.lag_s = max(0.0, time.perf_counter() - ts)
            now = time.monotonic()
            self._last_block = now
            if self.on_level and now - last_emit > 0.05:
                last_emit = now
                try:
                    self.on_level(float(np.max(np.abs(blk))))
                except Exception:
                    pass
            with self._sink_lock:
                sink = self._sink
                if sink is not None:
                    try:
                        sink(blk)
                    except Exception:
                        log.exception("Erro no processamento de áudio")
                elif self._keep_backlog:
                    self.buffer_block(blk)

    def _watch(self, gen: int) -> None:
        while self._running and gen == self._gen:
            time.sleep(3.0)
            if not (self._running and gen == self._gen):
                break
            stale = time.monotonic() - self._last_block > 3.0
            if not self.ok or stale:
                log.warning("Microfone parou; tentando reabrir")
                self._open()

    # ---- uso -----------------------------------------------------------
    def buffer_block(self, block: np.ndarray, prepend: bool = False) -> None:
        """Preserva áudio que chegou depois do fim de uma captura, em ordem."""
        with self._sink_lock:
            if prepend:
                self._backlog.insert(0, block.copy())
            else:
                self._backlog.append(block.copy())
            while sum(len(b) for b in self._backlog) > BACKLOG_MAX * BLOCK:
                del self._backlog[0]

    def set_sink(self, fn: Callable[[np.ndarray], None] | None, keep_backlog: bool = False) -> None:
        """fn=None + keep_backlog=True: os blocos seguintes ficam guardados (3 s) para o próximo use_sink(backlog=True)."""
        with self._sink_lock:               # espera o bloco em processamento terminar: corte limpo
            self._sink = fn
            self._keep_backlog = bool(keep_backlog and fn is None)
            self._backlog.clear()

    @contextmanager
    def use_sink(self, fn, backlog: bool = False, retain_backlog: bool = False):
        with self._sink_lock:
            prev = self._sink
            if backlog:
                pending, self._backlog = self._backlog, []
                for blk in pending:         # entrega o que chegou sem sink, ANTES de qualquer bloco novo
                    try:
                        fn(blk)
                    except Exception:
                        log.exception("Erro ao entregar o backlog de áudio")
            else:
                self._backlog.clear()
            self._sink, self._keep_backlog = fn, False
        try:
            yield
        finally:
            with self._sink_lock:
                self._sink = prev
                # Enquanto o STT interpreta um trecho de ativação, continue guardando
                # o comando que pode estar começando. A próxima captura o consome.
                self._keep_backlog = bool(retain_backlog and prev is None)

    def flush(self) -> None:
        with self._sink_lock:
            self._backlog.clear()
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
