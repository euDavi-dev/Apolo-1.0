import numpy as np


def activation_pcm() -> np.ndarray:
    """Sinal de 90 ms; usa a mesma saída do TTS para evitar abrir outro dispositivo."""
    rate, duration = 24000, 0.09
    t = np.arange(int(rate * duration), dtype=np.float32) / rate
    envelope = np.sin(np.pi * t / duration) ** 2
    signal = (np.sin(2 * np.pi * 880 * t) + 0.35 * np.sin(2 * np.pi * 1320 * t))
    return (signal * envelope * 4200).astype(np.int16)


def play_activation_sound(device=None) -> None:
    """Dois tons curtos de confirmação (bloqueia ~0,2 s)."""
    try:
        import sounddevice as sd
        sr = 24000
        def tone(f, d):
            t = np.linspace(0, d, int(sr * d), endpoint=False)
            env = np.minimum(1, np.minimum(t / 0.01, (d - t) / 0.04))
            return np.sin(2 * np.pi * f * t) * env
        wave = np.concatenate([tone(880, 0.07), tone(1320, 0.11)]).astype(np.float32) * 0.25
        sd.play(wave, sr, device=device, blocking=True)
    except Exception:
        pass
