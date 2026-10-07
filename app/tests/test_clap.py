"""Teste das palmas.

  python -m app.tests.test_clap                 # ao vivo: mostra níveis e eventos
  python -m app.tests.test_clap --calibrate     # mede o ambiente e sugere a sensibilidade
  python -m app.tests.test_clap --selftest      # testa o algoritmo com áudio sintético (sem microfone)
Opções: --sensitivity 0.30 --min-gap 100 --max-gap 1000 --cooldown 2.0 --device N
"""
import argparse
import sys
import time

import numpy as np

from app.core.config import Settings
from app.services.audio_engine import BLOCK, RATE, AudioEngine
from app.services.clap_detector import ClapCalibrator, ClapConfig, ClapDetector

BLOCK_MS = 20


def synth(events, seconds, noise=0.01, seed=1):
    """events: lista de (tempo_s, duração_s, amplitude, tipo 'clap'|'tone')."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    x = rng.normal(0, noise, n).astype(np.float32)
    for t0, dur, amp, kind in events:
        i0, m = int(t0 * RATE), int(dur * RATE)
        tt = np.arange(m) / RATE
        if kind == "clap":
            x[i0:i0 + m] += (rng.normal(0, 1, m) * np.exp(-tt * 90) * amp).astype(np.float32)
        else:
            x[i0:i0 + m] += (np.sin(2 * np.pi * 300 * tt) * amp).astype(np.float32)
    return np.clip(x, -1, 1)


def run_detector(x, cfg=None):
    det = ClapDetector(cfg or ClapConfig())
    out = []
    for i in range(0, len(x) - BLOCK + 1, BLOCK):
        ev = det.process(x[i:i + BLOCK])
        if ev:
            out.append(ev)
    return out


def selftest() -> int:
    cases = [
        ("duas palmas (400 ms)", [(1.0, .05, 1.2, "clap"), (1.4, .05, 1.2, "clap")], "double_clap", True),
        ("duas palmas rápidas (150 ms)", [(1.0, .05, 1.2, "clap"), (1.15, .05, 1.2, "clap")], "double_clap", True),
        ("uma palma só", [(1.0, .05, 1.2, "clap")], "double_clap", False),
        ("palmas muito distantes (2,5 s)", [(0.5, .05, 1.2, "clap"), (3.0, .05, 1.2, "clap")], "double_clap", False),
        ("tom sustentado de 600 ms (voz/música)", [(1.0, .6, 0.6, "tone"), (1.8, .6, 0.6, "tone")], "double_clap", False),
        ("tom sustentado deve ser rejeitado", [(1.0, .6, 0.6, "tone")], "rejected_long", True),
        ("som baixo (abaixo da sensibilidade)", [(1.0, .05, 0.12, "clap"), (1.4, .05, 0.12, "clap")], "double_clap", False),
    ]
    failed = 0
    for name, ev, expect, want in cases:
        got = expect in run_detector(synth(ev, 4.0))
        ok = got == want
        failed += not ok
        print(f"[{'OK' if ok else 'FALHOU'}] {name}")
    # cooldown: duas ativações seguidas dentro de 2 s contam só uma
    x = synth([(1.0, .05, 1.2, "clap"), (1.4, .05, 1.2, "clap"), (2.0, .05, 1.2, "clap"), (2.4, .05, 1.2, "clap")], 5.0)
    n = run_detector(x).count("double_clap")
    ok = n == 1
    failed += not ok
    print(f"[{'OK' if ok else 'FALHOU'}] cooldown bloqueia a 2ª ativação ({n} ativação)")
    print("\nTodos os testes passaram." if not failed else f"\n{failed} teste(s) falharam.")
    return 1 if failed else 0


def live(args) -> None:
    s = Settings.load()
    cfg = ClapConfig(args.sensitivity or s.clap_sensitivity, args.min_gap or s.clap_min_gap_ms,
                     args.max_gap or s.clap_max_gap_ms, args.cooldown or s.clap_cooldown_s)
    det = ClapDetector(cfg)
    last_ambient = [time.monotonic()]
    msgs = {"clap": "Audio peak detected", "double_clap": "Second clap detected\nAPOLO activated.",
            "rejected_long": "(som longo demais — não é palma)", "rejected_echo": "(eco ignorado)"}

    def sink(block):
        ev = det.process(block)
        if det.level >= max(0.05, det.noise * 2):
            print(f"Audio level: {det.level:.2f}")
        elif time.monotonic() - last_ambient[0] > 2:
            last_ambient[0] = time.monotonic()
            print(f"  (ambiente: {det.level:.2f} | piso de ruído {det.noise:.3f} | limiar {det.threshold:.2f})")
        if ev:
            print(msgs[ev])

    eng = AudioEngine(args.device if args.device is not None else s.input_device,
                      on_status=lambda ok, m: print("" if ok else f"ERRO: {m}"))
    if not eng.start():
        sys.exit(1)
    eng.set_sink(sink)
    print(f"Listening for claps...  (sensibilidade {cfg.sensitivity}, gap {cfg.min_gap_ms}-{cfg.max_gap_ms} ms, "
          f"cooldown {cfg.cooldown_s}s)  Ctrl+C para sair")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        eng.stop()


def calibrate(args) -> None:
    s = Settings.load()
    eng = AudioEngine(args.device if args.device is not None else s.input_device)
    if not eng.start():
        print("Microfone indisponível.")
        sys.exit(1)
    cal = ClapCalibrator()
    eng.set_sink(cal.feed)
    print("1/2  Fique em silêncio por 3 segundos...")
    time.sleep(3)
    cal.phase = "claps"
    print("2/2  Bata palmas 3 vezes (5 segundos)...")
    time.sleep(5)
    eng.stop()
    print(f"\nRuído ambiente (pico p95): {cal.noise_floor:.3f}")
    print(f"Palmas medidas: {len(cal.clap_peaks)}  {[round(p, 2) for p in cal.clap_peaks]}")
    print(f"Sensibilidade sugerida: {cal.result():.2f}  (defina em Configurações > Palmas)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--sensitivity", type=float)
    ap.add_argument("--min-gap", type=int)
    ap.add_argument("--max-gap", type=int)
    ap.add_argument("--cooldown", type=float)
    ap.add_argument("--device", type=int)
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    calibrate(a) if a.calibrate else live(a)
