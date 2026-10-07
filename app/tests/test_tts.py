"""python -m app.tests.test_tts  — testa a voz.

  python -m app.tests.test_tts                    # fala com o motor configurado e mostra o tempo até o 1º áudio
  python -m app.tests.test_tts --compare          # sintetiza as mesmas frases em cada motor, salva WAVs para você
                                                  # OUVIR e compara tempo de síntese e tempo real (RTF)
  python -m app.tests.test_tts --engine kokoro --voice bm_george --lang pt-br   # experimenta outra voz local
Os WAVs ficam em %APPDATA%\\APOLO\\tts_samples."""
import argparse
import sys
import time
import wave

from app.core.config import DATA_DIR, Settings
from app.services.tts_service import OUT_RATE, TTSService

SAMPLES = [
    ("curta", "São vinte e uma horas e trinta e seis minutos."),
    ("pontuação e números", "Hoje faz 27 graus em Salvador, com 80% de umidade. Leve um guarda-chuva, se quiser; ou não."),
    ("longa", "A fotossíntese é o processo pelo qual as plantas convertem luz solar em energia química. "
              "Em termos simples, elas capturam luz, absorvem água e gás carbônico, e produzem açúcar e oxigênio. "
              "É por isso que uma floresta funciona como um pulmão, ainda que um pouco desorganizado."),
]


def save_wav(path, pcm) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUT_RATE)
        w.writeframes(pcm.tobytes())


def compare(s: Settings, engines) -> None:
    out = DATA_DIR / "tts_samples"
    out.mkdir(parents=True, exist_ok=True)
    tts = TTSService(s)
    print(f"{'motor':<8}{'frase':<22}{'síntese':>9}{'áudio':>8}{'RTF':>7}")
    for eng in engines:
        for i, (name, text) in enumerate(SAMPLES):
            try:
                if eng == "kokoro" and i == 0:
                    tts._load_kokoro()                      # carga do modelo não conta como síntese
                pcm, rate, secs = tts.synthesize(text, eng)
            except Exception as e:
                print(f"{eng:<8}{name:<22}  indisponível: {e}")
                break
            dur = len(pcm) / rate
            path = out / f"{eng}_{i + 1}_{name.split()[0]}.wav"
            save_wav(path, pcm)
            print(f"{eng:<8}{name:<22}{secs:8.2f}s{dur:7.1f}s{secs / dur:7.2f}")
    print(f"\nWAVs salvos em: {out}\n(RTF < 1 = gera mais rápido que o tempo real; o que importa para a latência é a coluna 'síntese' da frase curta.)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--engine", choices=["edge", "kokoro", "sapi"])
    ap.add_argument("--voice", help="edge: nome da voz; kokoro: id da voz (pm_alex, bm_george...)")
    ap.add_argument("--lang", help="kokoro: idioma dos fonemas (pt-br, en-gb...)")
    a = ap.parse_args()
    s = Settings.load()
    if a.engine:
        s.tts_engine = a.engine
    if a.voice:
        if s.tts_engine == "kokoro":
            s.kokoro_voice = a.voice
        else:
            s.tts_voice = a.voice
    if a.lang:
        s.kokoro_lang = a.lang
    if a.compare:
        compare(s, ["edge", "kokoro"])
        return
    first = []
    tts = TTSService(s, on_level=lambda v: None, on_engine=lambda lbl: print("motor em uso:", lbl))
    tts.on_first_audio = first.append
    t0 = time.perf_counter()
    tts.speak("Olá, eu sou o APOLO. Todos os sistemas estão online.")
    tts.speak("A síntese de voz está funcionando.")
    tts.wait_idle(40)
    print(f"Motor: {s.tts_engine} | voz: {s.kokoro_voice if s.tts_engine == 'kokoro' else s.tts_voice} "
          f"| tempo total: {time.perf_counter() - t0:.1f}s")
    if first:
        print(f"Tempo até o 1º áudio: {(first[0] - t0) * 1000:.0f} ms")
    if tts.last_error:
        print("Aviso (caiu para o plano B):", tts.last_error)


if __name__ == "__main__":
    main()
