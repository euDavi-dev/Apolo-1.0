"""Cache em disco de frases já sintetizadas (áudio final, int16 mono 24 kHz).

Por quê: cada frase do edge-tts abre uma conexão nova com o servidor da Microsoft. Frases que se repetem
(confirmações de comando, "não entendi", falhas) não precisam disso: depois da 1ª vez saem do disco em milissegundos.
A chave inclui o motor e TODOS os parâmetros de voz; mudar voz, tom, ritmo ou graves invalida o cache sozinho.
Só vale para frases curtas; o cache é podado por uso (as menos usadas saem primeiro)."""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger("apolo.tts.cache")


class TTSCache:
    def __init__(self, directory: Path, max_files: int = 300, max_chars: int = 140):
        self.dir = Path(directory)
        self.max_files = max_files
        self.max_chars = max_chars
        self._lock = threading.Lock()

    def _path(self, sig: str, text: str) -> Path:
        h = hashlib.sha1(f"{sig}\n{text}".encode("utf-8")).hexdigest()[:24]
        return self.dir / f"{h}.npy"

    def cacheable(self, text: str) -> bool:
        return 0 < len(text) <= self.max_chars

    def has(self, sig: str, text: str) -> bool:
        return self.cacheable(text) and self._path(sig, text).is_file()

    def get(self, sig: str, text: str) -> np.ndarray | None:
        if not self.cacheable(text):
            return None
        path = self._path(sig, text)
        try:
            pcm = np.load(path, allow_pickle=False)
            if pcm.dtype != np.int16 or pcm.ndim != 1 or pcm.size < 100:
                raise ValueError("arquivo de cache inválido")
            os.utime(path)                          # marca como usado recentemente
            return pcm
        except FileNotFoundError:
            return None
        except Exception as e:
            log.warning("Cache de voz corrompido (%r); removendo", e)
            try:
                path.unlink()
            except OSError:
                pass
            return None

    def put(self, sig: str, text: str, pcm: np.ndarray) -> None:
        if not self.cacheable(text) or pcm.size < 100:
            return
        try:
            with self._lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                path = self._path(sig, text)
                tmp = path.with_suffix(".tmp")
                with open(tmp, "wb") as f:
                    np.save(f, np.asarray(pcm, dtype=np.int16), allow_pickle=False)
                os.replace(tmp, path)
                self._prune()
        except OSError as e:
            log.warning("Não consegui gravar no cache de voz: %r", e)

    def _prune(self) -> None:
        files = sorted(self.dir.glob("*.npy"), key=lambda f: f.stat().st_mtime)
        for f in files[:max(0, len(files) - self.max_files)]:
            try:
                f.unlink()
            except OSError:
                pass

    def clear(self) -> None:
        for f in self.dir.glob("*.np*"):
            try:
                f.unlink()
            except OSError:
                pass
