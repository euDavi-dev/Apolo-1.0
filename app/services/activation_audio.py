"""Respostas de ativação PRÉ-RENDERIZADAS ("Estou aqui.", "Pois não?"...).

Na execução normal NÃO há API, Gemini nem TTS: o áudio já está em memória (carregado no boot) e vai direto para a
saída (TTSService.play_pcm). Os WAVs são gerados UMA vez com o TTS e a voz configurados no APOLO
(scripts/generate_activation_audio.py, ou automaticamente em segundo plano no 1º boot / quando a voz muda).

    assets/sounds/activation/01_estou_aqui.wav ... 10_estou_a_escuta.wav  +  manifest.json
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from app.services.tts_service import OUT_RATE, to_rate
from app.utils.textutils import normalize

log = logging.getLogger("apolo.activation")
MANIFEST = "manifest.json"
_NUMBERED = re.compile(r"^\d{2}_.+\.wav$")


class ActivationGenError(Exception):
    pass


@dataclass
class Clip:
    name: str
    text: str
    pcm: np.ndarray        # int16 mono, OUT_RATE
    rate: int = OUT_RATE


def clip_filename(index: int, phrase: str) -> str:
    """1, 'À sua disposição.' -> '01_a_sua_disposicao.wav'"""
    return f"{index:02d}_{normalize(phrase).replace(' ', '_') or 'frase'}.wav"


def write_wav(path: Path, pcm: np.ndarray, rate: int) -> None:
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())
    os.replace(tmp, path)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """WAV 16-bit (qualquer taxa/canais) -> (int16 mono, taxa)."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("só WAV de 16 bits é suportado")
        ch, rate, raw = w.getnchannels(), w.getframerate(), w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        x = x[: len(x) // ch * ch].reshape(-1, ch).mean(axis=1).astype(np.int16)
    return x, rate


class ActivationAudio:
    def __init__(self, directory: Path, phrases: list[str], rng: random.Random | None = None):
        self.dir = Path(directory)
        self.phrases = list(phrases)
        self.rng = rng or random.Random()
        self.clips: list[Clip] = []
        self._last = -1

    # ---- arquivos ------------------------------------------------------------------------------
    def expected_files(self) -> list[Path]:
        return [self.dir / clip_filename(i, p) for i, p in enumerate(self.phrases, 1)]

    def missing(self) -> list[Path]:
        return [f for f in self.expected_files() if not f.is_file()]

    def manifest(self) -> dict:
        try:
            return json.loads((self.dir / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def needs_generation(self, request_signature: str | None = None) -> bool:
        """Faltam arquivos, as frases mudaram ou a voz configurada mudou desde a última geração."""
        if self.missing():
            return True
        m = self.manifest()
        if m.get("phrases") != self.phrases:
            return True
        return bool(request_signature and m.get("request_signature") != request_signature)

    # ---- memória ---------------------------------------------------------------------------------
    def load(self) -> int:
        """Lê os WAVs para a memória (chamado no boot). Devolve quantos foram carregados."""
        clips: list[Clip] = []
        for path, phrase in zip(self.expected_files(), self.phrases):
            if not path.is_file():
                continue
            try:
                pcm, rate = read_wav(path)
                if rate != OUT_RATE:
                    pcm = np.clip(to_rate(pcm.astype(np.float32), rate, OUT_RATE), -32768, 32767).astype(np.int16)
                if pcm.size < 100:
                    raise ValueError("áudio vazio")
                clips.append(Clip(path.name, phrase, pcm))
            except Exception as e:
                log.warning("[APOLO] Áudio de ativação inválido (%s): %r", path.name, e)
        self.clips, self._last = clips, -1
        return len(clips)

    def pick(self) -> Clip | None:
        """Escolhe uma resposta aleatória (sem repetir a anterior). Só memória; nunca toca o disco."""
        if not self.clips:
            return None
        choices = [i for i in range(len(self.clips)) if i != self._last] or [0]
        self._last = self.rng.choice(choices)
        return self.clips[self._last]


def generate(tts, phrases: list[str], directory: Path, request_signature: str | None = None,
             progress: Callable[[str], None] | None = None) -> dict:
    """Sintetiza TODAS as frases com o motor/voz configurados no APOLO e grava os WAVs + manifest.json.

    Usa um único motor para todas (não mistura vozes); `tts.engines()` decide quais são aceitáveis.
    `tts` precisa de engines(), synthesize(texto, motor) e voice_signature(motor) (TTSService)."""
    directory = Path(directory)
    engines = tts.engines()
    if not engines:
        raise ActivationGenError("O motor 'Voz do Windows' não gera arquivos. Escolha a voz Neural (edge) em Configurações > Voz.")
    errors: list[str] = []
    rendered: list[tuple[np.ndarray, int]] = []
    chosen = ""
    for engine in engines:
        try:
            out = []
            for i, phrase in enumerate(phrases, 1):
                if progress:
                    progress(f"Gerando {i}/{len(phrases)}: {phrase}")
                pcm, rate, _ = tts.synthesize(phrase, engine)
                out.append((pcm, rate))
            rendered, chosen = out, engine
            break
        except Exception as e:
            errors.append(f"{engine}: {e}")
            log.warning("[APOLO] Falha ao gerar áudios de ativação com %s: %r", engine, e)
    if not chosen:
        raise ActivationGenError("Não consegui sintetizar as respostas de ativação (" + "; ".join(errors) + "). "
                                 "O edge-tts precisa de internet nesta etapa; conecte-se e tente de novo.")
    directory.mkdir(parents=True, exist_ok=True)
    names = []
    for i, (phrase, (pcm, rate)) in enumerate(zip(phrases, rendered), 1):
        name = clip_filename(i, phrase)
        write_wav(directory / name, pcm, rate)
        names.append(name)
    for old in directory.glob("*.wav"):                       # frases antigas que saíram da lista
        if _NUMBERED.match(old.name) and old.name not in names:
            try:
                old.unlink()
            except OSError:
                pass
    manifest = {"version": 1, "engine": chosen, "voice_signature": tts.voice_signature(chosen),
                "request_signature": request_signature, "phrases": list(phrases), "files": names}
    (directory / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("[APOLO] %d respostas de ativação geradas com %s em %s", len(names), chosen, directory)
    return manifest
