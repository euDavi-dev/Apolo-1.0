"""Gera (ou regera) os áudios de ativação do APOLO com a MESMA voz configurada no app.

  python scripts/generate_activation_audio.py              # gera se faltar/estiver desatualizado
  python scripts/generate_activation_audio.py --force      # regera sempre
  python scripts/generate_activation_audio.py --list       # mostra frases e o estado dos arquivos

Saída: assets/sounds/activation/01_estou_aqui.wav ... + manifest.json.
O edge-tts (voz padrão) precisa de internet nesta etapa. Usa SOMENTE o motor/voz das Configurações (sem queda automática).
Depois de gerado, o APOLO usa os arquivos localmente, sem internet.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import ACTIVATION_DIR, ACTIVATION_PHRASES, Settings  # noqa: E402
from app.services.activation_audio import ActivationAudio, ActivationGenError, generate  # noqa: E402
from app.services.tts_service import TTSService  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="regera mesmo que os arquivos estejam em dia")
    ap.add_argument("--list", action="store_true", help="só lista frases e arquivos")
    args = ap.parse_args()

    cfg = Settings.load()
    tts = TTSService(cfg)
    store = ActivationAudio(ACTIVATION_DIR, ACTIVATION_PHRASES)
    sig = tts.voice_signature(cfg.tts_engine) if cfg.tts_engine != "sapi" else None

    print(f"Pasta: {ACTIVATION_DIR}")
    for f, phrase in zip(store.expected_files(), store.phrases):
        print(f"  [{'ok' if f.is_file() else '--'}] {f.name:<34} {phrase}")
    if args.list:
        return 0
    if not args.force and not store.needs_generation(sig):
        print("Tudo em dia (use --force para regerar).")
        return 0
    print(f"\nMotor: {cfg.tts_engine} · voz: {cfg.tts_voice if cfg.tts_engine == 'edge' else cfg.kokoro_voice}")
    try:
        m = generate(tts, store.phrases, store.dir, sig, progress=lambda t: print("  " + t))
    except ActivationGenError as e:
        print(f"\nERRO: {e}")
        return 1
    n = store.load()
    print(f"\nPronto: {len(m['files'])} arquivos gerados com '{m['engine']}' ({n} carregáveis).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
