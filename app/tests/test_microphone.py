"""python -m app.tests.test_microphone  — lista dispositivos e mostra o nível do microfone por 5 s."""
import sys
import time

import numpy as np

from app.core.config import Settings
from app.services.audio_engine import AudioEngine, list_input_devices, list_output_devices


def main() -> None:
    s = Settings.load()
    print("Entradas:")
    for i, n in list_input_devices():
        print(f"  [{i}] {n}{'   <- configurada' if i == s.input_device else ''}")
    print("Saídas:")
    for i, n in list_output_devices():
        print(f"  [{i}] {n}")
    peaks = []

    def sink(b):
        peaks.append(float(np.max(np.abs(b))))
        if len(peaks) % 5 == 0:
            lvl = max(peaks[-5:])
            print(f"\r{'█' * int(lvl * 50):<50} {lvl:.2f}", end="", flush=True)

    eng = AudioEngine(s.input_device, on_status=lambda ok, m: None if ok else print("ERRO:", m))
    if not eng.start():
        sys.exit("Não foi possível abrir o microfone.")
    eng.set_sink(sink)
    print("\nFale ou bata palmas (5 s)...")
    time.sleep(5)
    eng.stop()
    print(f"\n\nPico máximo: {max(peaks, default=0):.2f}  |  ruído típico: {float(np.median(peaks)):.3f}")
    print("OK: o microfone está captando." if max(peaks, default=0) > 0.02 else
          "Atenção: nível muito baixo. Verifique o microfone/volume de entrada do Windows.")


if __name__ == "__main__":
    main()
