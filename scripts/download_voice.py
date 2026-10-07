"""Baixa o modelo de voz local (Kokoro-82M, ONNX) para ./models/kokoro  —  python scripts/download_voice.py

Fonte oficial: releases do projeto kokoro-onnx (https://github.com/thewh1teagle/kokoro-onnx), tag model-files-v1.0.
Modelo Kokoro-82M (licença Apache 2.0). Só usa a biblioteca padrão do Python.

  python scripts/download_voice.py            # baixa kokoro-v1.0.onnx (~300 MB) e voices-v1.0.bin (vozes)
  python scripts/download_voice.py --check    # só confere se os arquivos já estão no lugar
  python scripts/download_voice.py --dir D:\\vozes\\kokoro
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
MIN_BYTES = {"kokoro-v1.0.onnx": 50_000_000, "voices-v1.0.bin": 1_000_000}   # detecta download truncado/página de erro


def target_dir(arg: str | None) -> Path:
    return Path(arg) if arg else Path(__file__).resolve().parents[1] / "models" / "kokoro"


def present(d: Path) -> bool:
    return all((d / f).is_file() and (d / f).stat().st_size >= MIN_BYTES[f] for f in FILES)


def download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=30) as r, open(tmp, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {dest.name}: {done / 1e6:6.1f} / {total / 1e6:.1f} MB", end="", flush=True)
    print()
    if tmp.stat().st_size < MIN_BYTES[dest.name]:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{dest.name} veio menor que o esperado; tente de novo")
    tmp.replace(dest)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", help="pasta de destino (padrão: ./models/kokoro)")
    ap.add_argument("--check", action="store_true", help="só verifica se os arquivos existem")
    a = ap.parse_args()
    d = target_dir(a.dir)
    if a.check:
        print(f"{'OK' if present(d) else 'FALTANDO'}: {d}")
        return 0 if present(d) else 1
    d.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        dest = d / f
        if dest.is_file() and dest.stat().st_size >= MIN_BYTES[f]:
            print(f"  {f}: já existe, pulando")
            continue
        print(f"Baixando {BASE}{f}")
        try:
            download(BASE + f, dest)
        except Exception as e:
            print(f"\nFalhou: {e}\nBaixe manualmente em {BASE} e coloque em {d}", file=sys.stderr)
            return 1
    print(f"Pronto: {d}\nPara usar: Configurações > Voz > Motor de voz > Local (Kokoro).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
