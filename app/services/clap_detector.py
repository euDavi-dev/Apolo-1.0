"""Detector local de duas palmas.

Não basta "volume > limiar". Cada bloco de áudio (20 ms) passa por estas checagens:

1. INTENSIDADE  — pico >= max(sensibilidade, ruído_ambiente x attack_ratio).
2. DURAÇÃO      — uma palma é um transiente curto (<= max_clap_ms). Fala, música e
                  ruídos contínuos ficam acima do limiar por mais tempo e são rejeitados.
3. INTERVALO    — a 2ª palma deve vir entre min_gap_ms e max_gap_ms após a 1ª.
                  Menos que min_gap = eco/reverberação da mesma palma (ignorado).
4. RUÍDO AMBIENTE — o piso de ruído é estimado continuamente (só em blocos "quietos").
5. COOLDOWN     — depois de uma ativação, novas detecções ficam bloqueadas.
6. CALIBRAÇÃO   — ClapCalibrator sugere a sensibilidade medindo o ambiente e suas palmas.

O tempo é contado pelos blocos processados (não pelo relógio), então o detector é
determinístico e pode ser testado com áudio sintético.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class ClapConfig:
    sensitivity: float = 0.30      # pico mínimo (0..1)
    min_gap_ms: int = 100
    max_gap_ms: int = 1000
    cooldown_s: float = 2.0
    max_clap_ms: int = 160         # picos mais longos que isso não são palmas
    attack_ratio: float = 4.0      # pico precisa superar N x o ruído ambiente
    decay_ratio: float = 0.5       # o pico "acabou" quando cai abaixo de limiar x isto


class ClapDetector:
    """process(bloco) -> None | "clap" | "double_clap" | "rejected_long" | "rejected_echo"."""

    def __init__(self, cfg: ClapConfig | None = None, block_ms: float = 20.0):
        self.cfg = cfg or ClapConfig()
        self.block_ms = block_ms
        self.reset()

    def reset(self, cooldown_s: float = 0.0) -> None:
        self.t = 0.0
        self.noise = 0.01
        self.level = 0.0
        self.in_peak = False
        self.peak_start = 0.0
        self.peak_rejected = False
        self.first_clap: float | None = None
        self.cooldown_until = cooldown_s * 1000.0

    @property
    def threshold(self) -> float:
        return max(self.cfg.sensitivity, self.noise * self.cfg.attack_ratio)

    def process(self, block: np.ndarray) -> str | None:
        cfg = self.cfg
        self.t += self.block_ms
        peak = float(np.max(np.abs(block))) if len(block) else 0.0
        self.level = peak
        thr = self.threshold
        event = None

        if self.in_peak:
            if peak < thr * cfg.decay_ratio:  # o pico terminou
                self.in_peak = False
                if not self.peak_rejected:
                    duration = self.t - self.peak_start
                    event = self._register(self.peak_start) if duration <= cfg.max_clap_ms else "rejected_long"
            elif not self.peak_rejected and self.t - self.peak_start > cfg.max_clap_ms:
                self.peak_rejected = True  # som sustentado (voz, música...)
                event = "rejected_long"
        elif peak >= thr and self.t >= self.cooldown_until:
            self.in_peak = True
            self.peak_start = self.t
            self.peak_rejected = False
        else:
            rms = float(np.sqrt(np.mean(block * block))) if len(block) else 0.0
            rate = 0.2 if rms < self.noise else 0.01  # cai rápido, sobe devagar
            self.noise = min(0.25, max(0.002, self.noise + rate * (rms - self.noise)))
        return event

    def _register(self, t_clap: float) -> str:
        cfg = self.cfg
        if self.first_clap is not None:
            gap = t_clap - self.first_clap
            if gap < cfg.min_gap_ms:
                return "rejected_echo"
            if gap <= cfg.max_gap_ms:
                self.first_clap = None
                self.cooldown_until = self.t + cfg.cooldown_s * 1000.0
                return "double_clap"
        self.first_clap = t_clap  # 1ª palma (ou a anterior expirou)
        return "clap"


class ClapCalibrator:
    """Fase 1: silêncio (mede o ruído). Fase 2: o usuário bate palmas (mede a força)."""

    def __init__(self):
        self.phase = "noise"
        self.noise_peaks: list[float] = []
        self.clap_peaks: list[float] = []
        self._cur = 0.0
        self._in = False

    def feed(self, block: np.ndarray) -> None:
        peak = float(np.max(np.abs(block))) if len(block) else 0.0
        if self.phase == "noise":
            self.noise_peaks.append(peak)
            return
        floor = max(0.08, 3 * self.noise_floor)
        if not self._in and peak >= floor:
            self._in, self._cur = True, peak
        elif self._in:
            self._cur = max(self._cur, peak)
            if peak < floor * 0.5:
                self._in = False
                self.clap_peaks.append(self._cur)

    @property
    def noise_floor(self) -> float:
        return float(np.percentile(self.noise_peaks, 95)) if self.noise_peaks else 0.02

    def result(self) -> float:
        noise = self.noise_floor
        if self.clap_peaks:
            clap = float(np.median(self.clap_peaks))
            value = max(noise * 3.0, clap * 0.5)
        else:
            value = max(noise * 4.0, 0.15)
        return round(min(0.8, max(0.08, value)), 2)
