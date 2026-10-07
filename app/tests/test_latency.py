"""python -m app.tests.test_latency  — mede Gemini + TTS ponta a ponta (sem tocar o áudio) em perguntas reais.

Para cada pergunta: 1º trecho do Gemini, 1ª frase pronta, e quanto o TTS leva para produzir o 1º áudio dessa frase.
Mostra "texto pronto -> 1º áudio" (o que o APOLO controla). STT e fim de fala dependem do microfone: veja o log
do APOLO em uso real (%APPDATA%\\APOLO\\apolo.log), onde cada turno imprime a tabela completa."""
import argparse
import asyncio
import statistics
import sys
import time

from app.core.config import Settings
from app.services.gemini_service import GeminiError, GeminiService
from app.services.tts_service import TTSService
from app.utils.textutils import clean_for_tts, pop_sentences

QUESTIONS = ["Qual a capital do Brasil?", "Quem escreveu Dom Casmurro?",
             "Explique rapidamente o que é inflação.", "Me dê uma dica para estudar melhor."]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["edge", "kokoro"], help="força o motor de voz")
    ap.add_argument("-n", type=int, default=len(QUESTIONS))
    a = ap.parse_args()
    s = Settings.load()
    if a.engine:
        s.tts_engine = a.engine
    g, tts = GeminiService(s), TTSService(s)
    loop = asyncio.new_event_loop()
    rows = []
    try:
        g.check()
        eng = s.tts_engine if s.tts_engine != "sapi" else "edge"
        if eng == "kokoro":
            tts._load_kokoro()
        print(f"Gemini: {s.gemini_model} | voz: {eng}\n")
        print(f"{'pergunta':<42}{'1º token':>10}{'1ª frase':>10}{'TTS 1º áudio':>14}{'texto>áudio':>13}")
        for q in QUESTIONS[:a.n]:
            g.reset()
            t0 = time.perf_counter()
            buf, t_tok, t_sent, first = "", None, None, None
            for d in g.stream_reply(q):
                t_tok = t_tok or time.perf_counter() - t0
                buf += d
                sents, buf = pop_sentences(buf, first=True)
                if sents and first is None:
                    t_sent = time.perf_counter() - t0
                    first = clean_for_tts(sents[0])
                    break
            first = first or clean_for_tts(buf)
            t_sent = t_sent or time.perf_counter() - t0
            t1 = time.perf_counter()
            tts._synth_with(eng, loop, first)
            t_tts = time.perf_counter() - t1
            rows.append((t_tok, t_sent, t_tts))
            print(f"{q[:40]:<42}{t_tok * 1000:9.0f}ms{t_sent * 1000:9.0f}ms{t_tts * 1000:13.0f}ms{(t_sent + t_tts) * 1000:12.0f}ms")
        print(f"\nMédia: 1º token {statistics.mean(r[0] for r in rows) * 1000:.0f} ms | "
              f"1ª frase {statistics.mean(r[1] for r in rows) * 1000:.0f} ms | "
              f"TTS {statistics.mean(r[2] for r in rows) * 1000:.0f} ms | "
              f"texto>áudio {statistics.mean(r[1] + r[2] for r in rows) * 1000:.0f} ms")
    except GeminiError as e:
        sys.exit(f"ERRO ({e.kind}): {e.message}")


if __name__ == "__main__":
    main()
