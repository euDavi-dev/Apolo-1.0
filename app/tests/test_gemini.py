"""python -m app.tests.test_gemini  — valida a chave/modelo e mede a latência do streaming (1º trecho e total)."""
import sys
import time

from app.core.config import Settings
from app.services.gemini_service import GeminiError, GeminiService


def main() -> None:
    s = Settings.load()
    g = GeminiService(s)
    print(f"Modelo: {s.gemini_model} | thinking: {s.gemini_thinking}")
    try:
        t0 = time.perf_counter()
        g.check()
        print(f"Chave e modelo OK ({(time.perf_counter() - t0) * 1000:.0f} ms)")
        for q in ("Olá! Quem é você? Responda em uma frase.", "Qual a capital do Brasil?"):
            t0 = time.perf_counter()
            first = None
            print(f"\nVocê: {q}\nAPOLO: ", end="", flush=True)
            for chunk in g.stream_reply(q):
                if first is None:
                    first = time.perf_counter() - t0
                print(chunk, end="", flush=True)
            print(f"\n\nPrimeiro trecho em {first * 1000:.0f} ms | total {(time.perf_counter() - t0) * 1000:.0f} ms")
    except GeminiError as e:
        sys.exit(f"\nERRO ({e.kind}): {e.message}")


if __name__ == "__main__":
    main()
