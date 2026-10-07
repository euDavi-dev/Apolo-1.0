"""python -m app.tests.test_stt_bench  — mede o STT em ETAPAS e compara motores com a SUA voz, microfone e internet.

Passo 1  (uma vez)   python -m app.tests.test_stt_bench --record      lê 5 frases na tela e grava WAVs
Passo 2              python -m app.tests.test_stt_bench                compara os motores nos mesmos áudios
                     python -m app.tests.test_stt_bench --engines google,whisper,gemini --runs 3
Rede                 python -m app.tests.test_stt_bench --probe        DNS + conexão até o Google (sem enviar áudio)

Para cada motor mostra: latência (média/mín/máx; a 1ª rodada é "fria" e vem separada), e no Google divide em
PREPARO DO ÁUDIO (flac.exe) x REDE+SERVIDOR; mostra também o tamanho enviado e a precisão (WER, % de palavras erradas
contra o texto que você leu; menor é melhor). Nada é inventado: o que não puder ser medido aparece como 'n/d'."""
from __future__ import annotations

import argparse
import json
import re
import socket
import statistics
import sys
import time
import unicodedata
import wave

from app.core.config import DATA_DIR, Settings

BENCH_DIR = DATA_DIR / "stt_bench"
REFS = [
    "que horas são agora",
    "abra o navegador por favor",
    "qual é a capital da França",
    "me explique rapidamente o que é inflação",
    "apolo você consegue me dizer como está o tempo em Salvador hoje",
]


def norm(t: str) -> list[str]:
    t = unicodedata.normalize("NFD", t.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]+", " ", t).split()


def wer(ref: str, hyp: str) -> float:
    """Word Error Rate (distância de edição entre palavras / nº de palavras da referência)."""
    r, h = norm(ref), norm(hyp)
    if not r:
        return 0.0 if not h else 1.0
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
            prev = cur
    return d[len(h)] / len(r)


def stats(vals: list[float]) -> str:
    return "n/d" if not vals else f"{statistics.mean(vals):5.2f}s (mín {min(vals):.2f}, máx {max(vals):.2f})"


def read_wav(path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getsampwidth() == 2 and w.getnchannels() == 1, "WAV deve ser 16 kHz mono 16 bit"
        return w.readframes(w.getnframes())


def probe(host: str = "www.google.com", n: int = 5) -> None:
    print(f"Rede até {host} ({n} tentativas)")
    for port in (80, 443):
        dns, tcp = [], []
        for _ in range(n):
            t0 = time.perf_counter()
            try:
                ip = socket.getaddrinfo(host, port, socket.AF_INET)[0][4][0]
                t1 = time.perf_counter()
                socket.create_connection((ip, port), timeout=5).close()
                dns.append(t1 - t0)
                tcp.append(time.perf_counter() - t1)
            except OSError as e:
                print(f"  porta {port}: falhou ({e})")
                break
        if tcp:
            print(f"  porta {port}:  DNS {stats(dns)}   conexão TCP {stats(tcp)}")
    print("Cada transcrição pelo Google abre uma conexão nova; a conexão TCP acima (e o TLS, se houver) é paga TODA vez.")


def record() -> None:
    from app.services.audio_engine import AudioEngine
    from app.services.speech_service import SpeechService, pcm_to_wav
    s = Settings.load()
    eng = AudioEngine(s.input_device, on_status=lambda ok, m: None if ok else print("ERRO:", m))
    if not eng.start():
        sys.exit("Não consegui abrir o microfone.")
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    svc = SpeechService(s)
    for i, ref in enumerate(REFS, 1):
        input(f"\n[{i}/{len(REFS)}] Aperte Enter e DEPOIS leia em voz natural:\n    \"{ref}\"\n")
        eng.flush()
        cap = svc._new_capture(clock=lambda: eng.block_ts)
        with eng.use_sink(cap.feed):
            cap.done.wait(timeout=25)
        if cap.result is None:
            print("  não ouvi nada; rode de novo.")
            continue
        st = cap.stats()
        print(f"  gravado: {st['audio_s']:.1f}s de áudio, {st['voiced_s']:.1f}s com voz, terminou por {st['fim_por']}, "
              f"atraso máx. do mic {st['lag_max_ms']:.0f} ms")
        (BENCH_DIR / f"{i:02d}.wav").write_bytes(pcm_to_wav(cap.result))
    (BENCH_DIR / "refs.json").write_text(json.dumps(REFS, ensure_ascii=False), encoding="utf-8")
    eng.stop()
    print(f"\nPronto: {BENCH_DIR}\nAgora rode:  python -m app.tests.test_stt_bench")


def bench(engines: list[str], runs: int) -> int:
    from app.services.gemini_service import GeminiService
    from app.services.speech_service import SpeechError, SpeechService
    refs_file = BENCH_DIR / "refs.json"
    if not refs_file.is_file():
        print("Nenhuma gravação encontrada. Rode primeiro:  python -m app.tests.test_stt_bench --record")
        return 1
    refs = json.loads(refs_file.read_text(encoding="utf-8"))
    clips = [(BENCH_DIR / f"{i:02d}.wav", r) for i, r in enumerate(refs, 1) if (BENCH_DIR / f"{i:02d}.wav").is_file()]
    print(f"{len(clips)} gravações em {BENCH_DIR}\n")
    for eng in engines:
        cfg = Settings.load()
        cfg.stt_engine = eng
        svc = SpeechService(cfg, GeminiService(cfg) if eng == "gemini" else None)
        cold, warm, prep, net, wers, kb, errors = [], [], [], [], [], [], 0
        t_load = None
        if eng == "whisper":                       # carregar o modelo é um custo de INICIALIZAÇÃO, não de pergunta
            print(f"[whisper] carregando o modelo '{cfg.whisper_model}' (na 1ª vez ele é BAIXADO: pode levar vários minutos; "
                  f"aguarde, não é travamento)...", flush=True)
            t0 = time.perf_counter()
            try:
                svc._load_whisper()
                t_load = time.perf_counter() - t0
                svc.warm()
            except SpeechError as e:
                print(f"[{eng}] indisponível: {e.message}\n")
                continue
        for path, ref in clips:
            pcm = read_wav(path)
            for k in range(runs):
                st: dict = {}
                try:
                    t0 = time.perf_counter()
                    text = svc.transcribe(pcm, st)
                    dt = time.perf_counter() - t0
                except SpeechError as e:
                    errors += 1
                    print(f"[{eng}] {path.name}: {e.kind}: {e.message}")
                    break
                (cold if (k == 0 and path == clips[0][0]) else warm).append(dt)
                if "flac_s" in st:
                    prep.append(st["flac_s"])
                    net.append(st.get("net_s", 0.0))
                if k == 0:
                    wers.append(wer(ref, text))
                    kb.append(st["pcm_kb"])
                    print(f"[{eng}] {path.name} ({st['audio_s']:.1f}s) {dt:5.2f}s  \"{text}\"")
        print(f"\n== {eng} ==")
        if t_load is not None:
            print(f"  carregar o modelo (só no boot): {t_load:.1f}s")
        print(f"  1ª transcrição (fria)      : {stats(cold)}")
        print(f"  demais ({len(warm)} medições)      : {stats(warm)}")
        if prep:
            print(f"  preparo do áudio (FLAC)    : {stats(prep)}")
            print(f"  rede + servidor            : {stats(net)}")
        if kb:
            print(f"  áudio enviado (PCM)        : {statistics.mean(kb):.0f} KB em média")
        print(f"  precisão: WER {statistics.mean(wers) * 100:.0f}%  (menor é melhor)" if wers else "  precisão: n/d")
        if errors:
            print(f"  falhas: {errors}")
        print()
    print("Lembrete: a transcrição especulativa só consegue esconder, no máximo, (fim do silêncio − início da especulação) = "
          "600 − 320 = 280 ms. Para ganhar mais é preciso um STT mais rápido, não mais especulação.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", action="store_true", help="grava 5 frases de referência")
    ap.add_argument("--probe", action="store_true", help="mede DNS/conexão até o Google")
    ap.add_argument("--engines", default="google", help="lista separada por vírgula: google,whisper,gemini")
    ap.add_argument("--runs", type=int, default=3)
    a = ap.parse_args()
    if a.probe:
        probe()
        return 0
    if a.record:
        record()
        return 0
    return bench([e.strip() for e in a.engines.split(",") if e.strip()], max(1, a.runs))


if __name__ == "__main__":
    sys.exit(main())
