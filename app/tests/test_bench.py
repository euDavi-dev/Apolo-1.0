"""python -m app.tests.test_bench  — benchmark do pipeline texto -> Gemini -> TTS -> 1º áudio, com média/mín/máx.

  python -m app.tests.test_bench                 # 5 perguntas curtas + 5 médias + 5 comandos (toca o áudio de verdade)
  python -m app.tests.test_bench --mute          # só sintetiza (sem alto-falante): mede até o áudio estar pronto
  python -m app.tests.test_bench --engine kokoro # compara o motor local
Comandos NÃO são executados (nada abre nem bloqueia): mede-se a confirmação falada. Eles rodam duas vezes: a 1ª com o
cache de voz vazio e a 2ª com ele cheio, para você ver o efeito do cache.

Mede "texto pronto -> 1º áudio". O STT (fim da fala -> texto) vem do log do APOLO ou do test_stt_bench; some os dois.
Os resultados também vão para %APPDATA%\\APOLO\\bench_results.csv."""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
import time

from app.core.config import DATA_DIR, Settings
from app.utils.textutils import clean_for_tts, pop_sentences

SHORT = ["Qual a capital do Brasil?", "Quem escreveu Dom Casmurro?", "Quanto é doze vezes oito?",
         "Em que ano o homem pisou na Lua?", "Qual o maior planeta do sistema solar?"]
MEDIUM = ["Explique rapidamente o que é inflação.", "Como funciona a fotossíntese?",
          "Me dê três dicas para estudar melhor.", "Qual a diferença entre vírus e bactéria?",
          "Por que o céu é azul?"]
COMMANDS = ["abra o Chrome", "abra a calculadora", "abra o bloco de notas", "abra o explorador de arquivos", "bloqueie o computador"]
COLS = ["gemini_1o_token", "gemini_1a_frase", "tts_sintese", "texto_ate_audio"]


def agg(rows: list[dict], key: str) -> str:
    v = [r[key] for r in rows if r.get(key) is not None]
    return "     n/d" if not v else f"{statistics.mean(v) * 1000:6.0f} / {min(v) * 1000:5.0f} / {max(v) * 1000:5.0f} ms"


def run_one(text: str, gemini, tts, mute: bool) -> dict:
    """Uma pergunta ao Gemini + a 1ª frase no TTS. Devolve tempos em segundos."""
    gemini.reset()
    t0 = time.perf_counter()
    tok = sent = None
    buf, first = "", None
    for d in gemini.stream_reply(text):
        tok = tok if tok is not None else time.perf_counter() - t0
        buf += d
        sents, buf = pop_sentences(buf, first=True)
        if sents:
            sent, first = time.perf_counter() - t0, clean_for_tts(sents[0])
            break
    if first is None:
        sent, first = time.perf_counter() - t0, clean_for_tts(buf)
    return {"gemini_1o_token": tok, "gemini_1a_frase": sent, "info": dict(getattr(gemini, "last_info", {}) or {}),
            **speak(first, tts, mute), "_t_text": sent}


def speak(phrase: str, tts, mute: bool) -> dict:
    """Tempo de 'texto pronto -> 1º áudio' para uma frase (toca de verdade, ou só sintetiza com --mute)."""
    if mute:
        rt: dict = {}
        t1 = time.perf_counter()
        tts._synthesize_item(tts._loop, phrase, rt)
        dt = time.perf_counter() - t1
        return {"tts_sintese": dt, "texto_ate_audio": dt, "cache": rt.get("cache_hit"), "rt": rt}
    first = []
    tts.on_first_audio = first.append
    t1 = time.perf_counter()
    tts.speak(phrase)
    tts.wait_idle(60)
    rt = dict(tts.timing)
    synth = (rt["synth_end"] - rt["synth_start"]) if "synth_end" in rt else None
    return {"tts_sintese": synth, "texto_ate_audio": (first[0] - t1) if first else None, "cache": rt.get("cache_hit"), "rt": rt}


def detail(r: dict) -> str:
    """Por dentro: Gemini (pedaços, raciocínio, humor) e TTS (conexão, resto, decodificação, pós)."""
    parts = []
    g = r.get("info") or {}
    if g:
        tc = g.get("t_chunks") or []
        parts.append(f"gemini: {g.get('chunks', 0)} pedaços ({', '.join(f'{t * 1000:.0f}' for t in tc)} ms), raciocínio "
                     f"{g.get('thoughts_tokens') if g.get('thoughts_tokens') is not None else 'n/d'} tok, humor {'sim' if g.get('humor') else 'não'}")
    rt = r.get("rt") or {}
    if rt.get("cache_hit"):
        parts.append("tts: CACHE")
    elif "edge_first_chunk_s" in rt:
        parts.append(f"tts edge: conexão+1º pedaço {rt['edge_first_chunk_s'] * 1000:.0f} | resto {rt['edge_stream_s'] * 1000:.0f} | "
                     f"decodificar {rt['edge_decode_s'] * 1000:.0f} | pós {rt.get('post_s', 0) * 1000:.0f} ms "
                     f"({rt.get('edge_audio_s', 0):.1f}s de áudio)")
    return "      " + " || ".join(parts) if parts else ""


def report(title: str, rows: list[dict]) -> None:
    print(f"\n{title}   (média / menor / maior)")
    for key, label in (("gemini_1o_token", "Gemini 1º token"), ("gemini_1a_frase", "Gemini 1ª frase"),
                       ("tts_sintese", "TTS síntese 1ª frase"), ("texto_ate_audio", "TEXTO PRONTO -> 1º ÁUDIO")):
        if any(r.get(key) is not None for r in rows):
            print(f"  {label:<26}{agg(rows, key)}")
    if any("gemini_1a_frase" in r for r in rows):
        tot = [r["gemini_1a_frase"] + r["texto_ate_audio"] for r in rows if r.get("texto_ate_audio") is not None]
        if tot:
            print(f"  {'Gemini + TTS (enviado -> 1º áudio)':<26}{statistics.mean(tot) * 1000:6.0f} / {min(tot) * 1000:5.0f} / {max(tot) * 1000:5.0f} ms")


def run_all(gemini, tts, mute: bool, commands_ack, only: str = "") -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for group, items in (("curta", SHORT), ("média", MEDIUM)):
        if only and only not in (group, group.replace("é", "e")):
            continue
        rows = []
        for q in items:
            r = run_one(q, gemini, tts, mute)
            print(f"  [{group}] {q:<46} 1º tok {r['gemini_1o_token'] * 1000:5.0f} ms | 1ª frase {r['gemini_1a_frase'] * 1000:5.0f} ms "
                  f"| texto→áudio {(r['texto_ate_audio'] or 0) * 1000:5.0f} ms")
            if detail(r):
                print(detail(r))
            rows.append(r)
            out.append((group, r))
        report(f"Perguntas {group}s", rows)
    for rodada in ("1ª rodada (cache vazio)", "2ª rodada (cache cheio)"):
        if only and only not in ("comandos", "comando"):
            break
        rows = []
        for c in COMMANDS:
            r = speak(commands_ack(c), tts, mute)
            rows.append(r)
            out.append((f"comando {rodada[:2]}", r))
        report(f"Comandos — {rodada}", rows)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mute", action="store_true")
    ap.add_argument("--engine", choices=["edge", "kokoro"])
    ap.add_argument("--only", choices=["curta", "media", "média", "comandos"], help="roda só um grupo")
    a = ap.parse_args()
    from app.services.command_service import CommandService
    from app.services.gemini_service import GeminiError, GeminiService
    from app.services.tts_service import TTSService
    import asyncio
    s = Settings.load()
    if a.engine:
        s.tts_engine = a.engine
    g, tts = GeminiService(s), TTSService(s)
    tts._loop = asyncio.new_event_loop()
    cs = CommandService(ack=g.persona.open_ack)
    labels = {c.label.lower(): c for c in cs.commands if c.label}

    def ack(phrase: str) -> str:                   # confirmação que o APOLO falaria (sem executar nada)
        parsed = cs.parse(phrase)
        if not parsed:
            return "Bloqueando o computador."
        cmd, _ = parsed
        return cmd.reply or g.persona.open_ack(cmd.label)
    try:
        g.check()
        if s.tts_engine == "kokoro":
            tts._load_kokoro()
        tts.cache.clear()
        print(f"Gemini {s.gemini_model} | voz {s.tts_engine} | {'sem tocar' if a.mute else 'tocando'}\n")
        rows = run_all(g, tts, a.mute, ack, a.only or "")
    except GeminiError as e:
        sys.exit(f"ERRO ({e.kind}): {e.message}")
    path = DATA_DIR / "bench_results.csv"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["grupo"] + COLS + ["cache"])
        for grp, r in rows:
            w.writerow([grp] + [f"{r.get(c):.4f}" if r.get(c) is not None else "" for c in COLS] + [r.get("cache")])
    print(f"\nResultados em {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
