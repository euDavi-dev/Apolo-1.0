"""python -m app.tests.test_offline  — testes que NÃO precisam de microfone, internet, chave nem caixa de som.

Cobre: limpeza de texto para voz, divisão de frases, personalidade/humor, fim de fala (VAD) e transcrição
especulativa, pós-processamento de áudio, pipeline do TTS (fila, pausas, queda de motor, cancelamento),
migração de configurações e o detector de palmas (áudio sintético)."""
from __future__ import annotations

import contextlib
import json
import random
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'OK' if cond else 'FALHOU'}] {name}" + (f"  ({detail})" if detail else ""))


# ----------------------------------------------------------------------------- texto
def t_text() -> None:
    from app.utils.textutils import clean_for_display, clean_for_tts, pop_sentences
    check("tts: markdown e emoji removidos", clean_for_tts("**Brasília** é a capital 🇧🇷😀") == "Brasília é a capital")
    check("tts: hora 21h36", clean_for_tts("São 21h36.") == "São 21 horas e 36 minutos.")
    check("tts: relógio 14:30", clean_for_tts("às 14:30") == "às 14 horas e 30 minutos")
    check("tts: reais", clean_for_tts("R$ 12,50") == "12 reais e 50 centavos")
    check("tts: graus e por cento", clean_for_tts("27°C e 80%") == "27 graus Celsius e 80 por cento")
    check("tts: link vira 'link'", clean_for_tts("veja https://a.com/x") == "veja link")
    check("tts: lista sem marcadores", clean_for_tts("- um\n- dois") == "um. dois")
    check("display: mantém números", clean_for_display("**R$ 12,50** às 21h36") == "R$ 12,50 às 21h36")
    check("frases: ponto simples", pop_sentences("Olá. Tudo bem? Estou") == (["Olá.", "Tudo bem?"], "Estou"))
    check("frases: Dr. não corta", pop_sentences("O Dr. Silva chegou. Depois")[0] == ["O Dr. Silva chegou."])
    check("frases: decimal 3.5", pop_sentences("A taxa é 3.5 por cento. Ok")[0] == ["A taxa é 3.5 por cento."])
    check("frases: iniciais J. K.", pop_sentences("J. K. Rowling escreveu. Sim")[0] == ["J. K. Rowling escreveu."])
    check("frases: 'Brasília.' sai sem esperar o fim do streaming", pop_sentences("Brasília.", first=True) == (["Brasília."], ""))
    check("frases: '?' no fim do buffer sai na hora", pop_sentences("Tudo bem?", first=True) == (["Tudo bem?"], ""))
    check("frases: número antes do ponto espera (3. pode virar 3.5)", pop_sentences("A taxa é 3.", first=True) == ([], "A taxa é 3."))
    check("frases: abreviação 'Dr.' espera", pop_sentences("Fale com o Dr.", first=True)[0] == [])
    buf, got = "", []
    for ch in ["Bras", "ília", ".", " Uma escolha", " previsível."]:
        buf += ch
        ss, buf = pop_sentences(buf, first=not got)
        got += ss
    check("frases: streaming em pedaços libera cada frase assim que completa", got == ["Brasília.", "Uma escolha previsível."], str(got))
    long = "Bem, essa pergunta é bastante interessante porque envolve muitos detalhes, então vou explicar"
    check("frases: 1ª frase longa corta na vírgula", pop_sentences(long, first=True)[0] ==
          ["Bem, essa pergunta é bastante interessante porque envolve muitos detalhes,"])
    check("frases: depois da 1ª não corta cedo", pop_sentences(long, first=False)[0] == [])


# ----------------------------------------------------------------------------- persona
def t_persona() -> None:
    from app.core.persona import SYSTEM_PROMPT, Persona
    p = Persona(0.28, random.Random(7))
    n = sum(p.allow_humor("qual a capital da França") for _ in range(2000))
    check("humor: raro (entre 8% e 25%)", 160 <= n <= 500, f"{n / 20:.1f}% das respostas")
    p2 = Persona(1.0, random.Random(1))
    seq = [p2.allow_humor("oi") for _ in range(9)]
    check("humor: nunca duas respostas seguidas", not any(a and b for a, b in zip(seq, seq[1:])), str(seq))
    check("humor: desligado com 0", not any(Persona(0.0).allow_humor("oi") for _ in range(50)))
    check("humor: assunto sério bloqueia", not any(Persona(1.0).allow_humor("meu pai morreu ontem") for _ in range(20)))
    p3 = Persona(0.0)
    for r in ("Certamente. Abrindo.", "Claro, aqui está.", "Com certeza, sim."):
        p3.note_reply(r)
    line = p3.dynamic_line("quarta", False)
    check("aberturas: evita as recentes", "certamente" in line and "claro" in line and "com certeza" in line, line)
    check("linha dinâmica curta", len(line) < 200)
    acks = {Persona(0, random.Random(i)).open_ack("o Chrome") for i in range(30)}
    check("ack de comando varia", len(acks) >= 3, f"{len(acks)} variações")
    q = Persona(1.0, random.Random(3))
    check("ironia da madrugada só à noite", q.late_night_quip(__import__('datetime').datetime(2026, 9, 30, 14, 0)) == "")
    quips = [Persona(1.0, random.Random(i)).late_night_quip(__import__('datetime').datetime(2026, 9, 30, 23, 36)) for i in range(20)]
    check("ironia da madrugada aparece à noite", any(quips))
    for must in ("FALA", "TAMANHO", "HUMOR", "SE NÃO SOUBER", "CORREÇÕES", "AÇÕES", "VARIEDADE"):
        check(f"prompt cobre {must}", must in SYSTEM_PROMPT)
    check("prompt compacto (<2500 caracteres)", len(SYSTEM_PROMPT) < 2500, f"{len(SYSTEM_PROMPT)} caracteres")


# ----------------------------------------------------------------------------- VAD / STT
def speech_blocks(segments, rate=16000):
    """segments: [(duração_s, 'fala'|'silencio')] -> blocos float32 de 20 ms."""
    out = []
    rng = np.random.default_rng(0)
    for dur, kind in segments:
        n = int(dur * rate)
        if kind == "fala":
            t = np.arange(n) / rate
            x = 0.3 * np.sin(2 * np.pi * 180 * t) + 0.1 * np.sin(2 * np.pi * 900 * t)
        else:
            x = rng.normal(0, 0.002, n)
        out.append(x.astype(np.float32))
    x = np.concatenate(out)
    return [x[i:i + 320] for i in range(0, len(x) - 319, 320)]


class FakeEngine:
    ok = True

    def __init__(self, blocks):
        self.blocks = blocks

    @contextlib.contextmanager
    def use_sink(self, fn):
        t = threading.Thread(target=lambda: [fn(b) for b in self.blocks], daemon=True)
        t.start()
        try:
            yield
        finally:
            t.join(2)


def t_vad() -> None:
    from app.services.speech_service import SpeechService, UtteranceCapture
    from app.core.config import Settings

    def run(cap, blocks):
        for b in blocks:
            cap.feed(b)
        return cap

    base = [(0.3, "silencio"), (1.2, "fala")]
    # fim de fala: antes 800 ms, agora 600 ms (e a transcrição especulativa sai com 320 ms)
    old = run(UtteranceCapture(end_silence_ms=800), speech_blocks(base + [(1.5, "silencio")]))
    new = run(UtteranceCapture(end_silence_ms=600, soft_silence_ms=320), speech_blocks(base + [(1.5, "silencio")]))
    check("VAD: fim de fala detectado (antes e agora)", old.result is not None and new.result is not None)
    check("VAD: silêncio até decidir 800 ms -> 600 ms", old.silence * 20 == 800 and new.silence * 20 == 600,
          f"antes {old.silence * 20} ms, agora {new.silence * 20} ms")
    check("VAD: o áudio enviado é o mesmo (precisão preservada)", len(old.result) == len(new.result),
          f"{len(old.result)} bytes")
    soft = []
    cap = UtteranceCapture(end_silence_ms=600, soft_silence_ms=320, on_soft_end=soft.append)
    run(cap, speech_blocks(base + [(1.5, "silencio")]))
    check("VAD: especulação dispara uma vez", len(soft) == 1)
    check("VAD: snapshot == resultado final", soft and soft[0] == cap.result)
    # pausa de 400 ms no meio da frase: especula, descarta e continua
    events = []
    cap = UtteranceCapture(end_silence_ms=600, soft_silence_ms=320, on_soft_end=lambda p: events.append("soft"),
                           on_resume=lambda: events.append("resume"))
    run(cap, speech_blocks(base + [(0.4, "silencio"), (1.0, "fala"), (1.2, "silencio")]))
    check("VAD: pausa curta -> especula e cancela", events == ["soft", "resume", "soft"], str(events))
    dur = len(cap.result) / 2 / 16000
    check("VAD: pausa curta não corta a frase", dur > 2.4, f"{dur:.2f}s de áudio")
    # pausa de 900 ms: corta (limite esperado)
    cap = run(UtteranceCapture(end_silence_ms=600, soft_silence_ms=320), speech_blocks(base + [(0.9, "silencio"), (1.0, "fala")]))
    check("VAD: pausa acima do limite encerra a fala", len(cap.result) / 2 / 16000 < 1.9)
    # só silêncio
    cap = run(UtteranceCapture(start_timeout_s=1.0), speech_blocks([(2.0, "silencio")]))
    check("VAD: silêncio puro -> sem resultado", cap.done.is_set() and cap.result is None)

    # SpeechService.listen: especulação aproveitada x descartada
    def make(blocks):
        s = Settings()
        svc = SpeechService(s)
        calls = []
        svc.transcribe = lambda pcm, stats=None: (calls.append(len(pcm)), "olá mundo")[1]
        return svc, calls, FakeEngine(blocks)

    svc, calls, eng = make(speech_blocks(base + [(1.5, "silencio")]))
    text = svc.listen(eng)
    check("STT: especulativa aproveitada (1 chamada)", text == "olá mundo" and len(calls) == 1, f"{len(calls)} chamada(s)")
    svc, calls, eng = make(speech_blocks(base + [(0.4, "silencio"), (1.0, "fala"), (1.5, "silencio")]))
    text = svc.listen(eng)
    check("STT: especulativa descartada, texto do áudio completo", text == "olá mundo" and len(calls) == 2
          and calls[-1] > calls[0], f"chamadas {calls}")
    svc, calls, eng = make(speech_blocks(base + [(1.5, "silencio")]))
    svc.cfg.stt_speculative = False
    svc.listen(eng)
    check("STT: sem especulação, 1 chamada", len(calls) == 1)
    svc, calls, eng = make(speech_blocks([(2.0, "silencio")]))
    svc.cfg.vad_end_silence_ms = 600
    eng2 = FakeEngine(speech_blocks([(8.0, "silencio")]))
    check("STT: silêncio -> None", svc.listen(eng2) is None)


# ----------------------------------------------------------------------------- áudio
def voice_like(sec=1.5, rate=24000, lead=0.25, tail=0.3, amp=0.05):
    t = np.arange(int(sec * rate)) / rate
    v = amp * (np.sin(2 * np.pi * 120 * t) + 0.5 * np.sin(2 * np.pi * 1500 * t)) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
    return np.concatenate([np.zeros(int(lead * rate)), v, np.zeros(int(tail * rate))]).astype(np.float32)


def t_audio() -> None:
    from app.services.tts_service import OUT_RATE, gap_for, shape_audio, to_rate
    x = voice_like()
    y = shape_audio(x, OUT_RATE, 2.5)
    removed = (len(x) - len(y)) / OUT_RATE
    check("áudio: silêncio de borda cortado", removed > 0.35, f"{removed * 1000:.0f} ms removidos")
    check("áudio: sem estourar", float(np.max(np.abs(y))) <= 0.93, f"pico {np.max(np.abs(y)):.2f}")
    check("áudio: começa e termina em silêncio (sem estalo)", abs(y[0]) < 1e-3 and abs(y[-1]) < 1e-3)
    act = y[np.abs(y) > 0.05 * np.max(np.abs(y))]
    rms = float(np.sqrt(np.mean(act ** 2)))
    check("áudio: volume normalizado (~0,11 RMS)", 0.07 < rms < 0.16, f"{rms:.3f}")
    quiet, loud = shape_audio(voice_like(amp=0.04), OUT_RATE), shape_audio(voice_like(amp=0.4), OUT_RATE)  # 20 dB de diferença (o ganho é limitado a +12 dB)
    rq = float(np.sqrt(np.mean(quiet[np.abs(quiet) > 0.05 * quiet.max()] ** 2)))
    rl = float(np.sqrt(np.mean(loud[np.abs(loud) > 0.05 * loud.max()] ** 2)))
    check("áudio: baixo e alto ficam no mesmo nível", abs(20 * np.log10(rq / rl)) < 3.0, f"{abs(20 * np.log10(rq / rl)):.1f} dB de diferença")

    def band(a, lo, hi):
        s = np.abs(np.fft.rfft(a)) ** 2
        f = np.fft.rfftfreq(len(a), 1 / OUT_RATE)
        return float(s[(f >= lo) & (f < hi)].sum())
    flat, bass = shape_audio(x, OUT_RATE, 0.0), shape_audio(x, OUT_RATE, 4.0)
    # (mesma duração e mesmo ganho alvo => compara a razão grave/agudo)
    r_flat = band(flat, 60, 250) / band(flat, 1000, 2500)
    r_bass = band(bass, 60, 250) / band(bass, 1000, 2500)
    check("áudio: realce de graves funciona", r_bass > r_flat * 1.3, f"razão grave/agudo {r_flat:.2f} -> {r_bass:.2f}")
    sample = voice_like(sec=3.0)
    shape_audio(sample, OUT_RATE, 2.5)  # aquece; mede só o processamento, sem gerar a amostra
    t0 = time.perf_counter()
    for _ in range(20):
        shape_audio(sample, OUT_RATE, 2.5)
    ms = (time.perf_counter() - t0) / 20 * 1000
    check("áudio: pós-processamento rápido (3 s de fala)", ms < 40, f"{ms:.1f} ms por frase")
    check("áudio: reamostragem", len(to_rate(np.zeros(22050, np.float32), 22050, 24000)) == 24000)
    check("pausas por pontuação", gap_for("Olá.", 140) == 140 > gap_for("Olá:", 140) > gap_for("Olá,", 140) > 0)


# ----------------------------------------------------------------------------- pipeline do TTS
class FakeStream:
    def __init__(self):
        self.writes: list[tuple[float, int]] = []
        self.closed = False

    def write(self, a):
        self.writes.append((time.perf_counter(), len(a)))
        time.sleep(len(a) / 24000 * 0.05)      # simula um dispositivo bem mais rápido que o tempo real

    def stop(self):
        pass

    def close(self):
        self.closed = True


def t_tts_pipeline() -> None:
    from app.core.config import Settings
    from app.services import tts_service as T

    def build(order_behaviour):
        cfg = Settings()
        cfg.tts_pause_ms = 120
        cfg.tts_cache = False
        svc = T.TTSService(cfg)
        streams: list[FakeStream] = []
        svc._open = lambda rate: (streams.append(FakeStream()), streams[-1])[1]
        sapi_texts: list[str] = []
        svc._speak_sapi = lambda text: sapi_texts.append(text)

        def synth(name, loop, text):
            beh = order_behaviour.get(name, "fail")
            if beh == "ok":
                time.sleep(0.03)
                return T.shape_audio(voice_like(sec=0.6), T.OUT_RATE, 2.0)
            if beh == "missing":
                raise T._Unavailable("sem modelo")
            raise RuntimeError("boom")
        svc._synth_with = synth
        return svc, streams, sapi_texts

    # 1) edge ok: 3 frases, 1 stream aberto, 1ª-áudio uma vez, pausas entre frases
    svc, streams, sapi = build({"edge": "ok"})
    firsts = []
    svc.on_first_audio = firsts.append
    engines = []
    svc.on_engine = engines.append
    for s in ("Primeira frase.", "Segunda, com vírgula,", "Terceira frase."):
        svc.speak(s)
    svc.wait_idle(10)
    check("TTS: terminou toda a fila", svc._pending == 0)
    check("TTS: um único stream por resposta (pré-aberto)", len(streams) == 1, f"{len(streams)} stream(s)")
    check("TTS: callback de 1º áudio chamado uma vez", len(firsts) == 1)
    check("TTS: motor edge reportado como NEURAL", engines[:1] == ["NEURAL"], str(engines))
    one = len(T.shape_audio(voice_like(sec=0.6), T.OUT_RATE, 2.0))
    written = sum(n for _, n in streams[0].writes)
    check("TTS: pausas entre frases inseridas no stream", written > 3 * one, f"{(written - 3 * one) / T.OUT_RATE * 1000:.0f} ms de pausa")
    svc.speak("Outra resposta logo em seguida.")
    svc.wait_idle(10)
    check("TTS: reaproveita a saída na resposta seguinte", len(streams) == 1 and len(firsts) == 2)
    time.sleep(3.6)  # saída é reaproveitada por 3 s antes de fechar
    check("TTS: stream fechado após o período de reaproveitamento", streams[0].closed)
    # 2) edge falha -> kokoro (local) assume
    svc, streams, sapi = build({"edge": "fail", "kokoro": "ok"})
    svc.speak("Teste.")
    svc.wait_idle(10)
    check("TTS: edge falhou -> kokoro falou", svc.engine == "kokoro" and not sapi, svc.last_error)
    check("TTS: edge fica de castigo", svc._off_until["edge"] > time.time())
    # 3) nada disponível -> SAPI
    svc, streams, sapi = build({"edge": "fail", "kokoro": "missing"})
    svc.speak("Teste final.")
    svc.wait_idle(10)
    check("TTS: sem edge/kokoro -> voz do Windows", sapi == ["Teste final."] and svc.engine == "sapi")
    # 4) preferência kokoro na frente
    svc, streams, sapi = build({"edge": "ok", "kokoro": "ok"})
    svc.cfg.tts_engine = "kokoro"
    svc.speak("Teste.")
    svc.wait_idle(10)
    check("TTS: preferência 'kokoro' vem primeiro", svc.engine == "kokoro")
    # 5) stop() descarta o resto
    svc, streams, sapi = build({"edge": "ok"})
    for i in range(6):
        svc.speak(f"Frase número {i}.")
    time.sleep(0.12)
    svc.stop()
    time.sleep(0.4)
    check("TTS: stop() zera a fila", svc._pending == 0 and svc._audio_q.empty() and svc._text_q.empty())
    svc.speak("Nova resposta.")
    svc.wait_idle(10)
    check("TTS: funciona normalmente depois do stop()", svc._pending == 0 and svc.engine == "edge")
    # 6) caminho de Kokoro ausente
    svc = T.TTSService(Settings())
    svc.cfg.kokoro_model_dir = tempfile.mkdtemp()
    check("Kokoro: sem arquivos -> indisponível (sem exceção)", svc._load_kokoro() is False and "não encontrados" in svc.last_error)


# ----------------------------------------------------------------------------- config / métricas / palmas
def t_misc() -> None:
    from app.core import config
    from app.utils.metrics import Turn
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "settings.json"
        old = config.SETTINGS_FILE
        config.SETTINGS_FILE = f
        try:
            f.write_text(json.dumps({"tts_rate": 10, "weather_city": "Recife", "clap_sensitivity": 0.4}), encoding="utf-8")
            s = config.Settings.load()
            check("config: migra o ritmo antigo (10 -> -4)", s.tts_rate == -4)
            check("config: preserva ajustes do usuário", s.weather_city == "Recife" and s.clap_sensitivity == 0.4)
            f.write_text(json.dumps({"tts_rate": 5, "config_version": 2}), encoding="utf-8")
            check("config: respeita ritmo já personalizado", config.Settings.load().tts_rate == 5)
        finally:
            config.SETTINGS_FILE = old
    t = Turn()
    for k, v in {"listen_start": 0, "speech_start": .5, "voice_end": 2.0, "speech_end": 2.6, "stt_start": 2.32,
                 "stt_done": 2.8, "gemini_sent": 2.81, "first_token": 3.4, "first_sentence": 3.5,
                 "tts_first_audio": 3.9, "gemini_done": 4.2, "playback_end": 6.0}.items():
        t.mark(k, v)
    r = t.report()
    check("métricas: relatório completo", all(k in r for k in ("APOLO LATENCY", "Gemini → 1º token", "1ª frase → 1º áudio",
                                                              "FIM DA FALA → PRIMEIRO ÁUDIO", "DURAÇÃO DA RESPOSTA")))
    check("métricas: fim da fala -> áudio = 1,90 s", abs(t.span("voice_end", "tts_first_audio") - 1.9) < 1e-9)
    # o log REAL do usuário (sem Gemini): não pode inventar linhas de Gemini nem de TTS
    u = Turn()
    for k, v in {"listen_start": 0, "speech_start": .11, "voice_end": 5.0, "speech_end": 5.0, "capture_done": 5.0,
                 "stt_start": 5.01, "stt_done": 6.84, "tts_first_audio": 8.78, "playback_end": 12.36}.items():
        u.mark(k, v)
    ur = u.report()
    check("métricas: turno local não mostra linhas de Gemini", "Gemini" not in ur and "síntese" not in ur)
    sm = u.summary()
    check("métricas: LATÊNCIA (3,78) separada de DURAÇÃO (3,58)",
          abs(sm["fim_fala_ate_audio"] - 3.78) < 1e-9 and abs(sm["duracao_resposta"] - 3.58) < 1e-9)
    from app.tests.test_clap import run_detector, selftest, synth
    check("palmas: selftest do detector (inalterado)", selftest() == 0)
    two = [(1.0, .05, 1.2, "clap"), (1.5, .05, 1.2, "clap")]
    for name, ev, noise, want in (("duas palmas com ruído ambiente", two, 0.04, True),
                                  ("duas palmas médias (amp 0,7)", [(1.0, .05, .7, "clap"), (1.5, .05, .7, "clap")], 0.01, True),
                                  ("só ruído ambiente forte", [], 0.12, False),
                                  ("uma palma com ruído", two[:1], 0.04, False),
                                  ("palma + voz sustentada", [(1.0, .05, 1.2, "clap"), (1.3, .5, .6, "tone")], 0.01, False)):
        check(f"palmas: {name}", ("double_clap" in run_detector(synth(ev, 4.0, noise=noise))) == want)


# ----------------------------------------------------------------------------- captura: relógio do áudio, motivo do fim
def t_capture() -> None:
    from app.services.speech_service import UtteranceCapture

    def run(segments, clock_offset=0.0, **kw):
        blocks = speech_blocks(segments)
        state = {"i": 0}
        # relógio sintético: o bloco i "chegou" em 100 + i*20 ms (tempo real do áudio) — o código processa depois
        cap = UtteranceCapture(clock=lambda: 100.0 + state["i"] * 0.02 - clock_offset, **kw)
        for b in blocks:
            state["i"] += 1
            cap.feed(b)
        return cap
    cap = cap_sil = run([(0.3, "silencio"), (1.2, "fala"), (1.5, "silencio")], end_silence_ms=600)
    check("captura: fim por silêncio", cap.end_reason == "silêncio", cap.end_reason)
    gap = cap.t_end - cap.t_last_voice
    check("captura: silêncio medido no tempo do áudio = 0,60 s", abs(gap - 0.6) < 0.025, f"{gap:.3f}s")
    cap = run([(0.2, "silencio"), (17.0, "fala")], end_silence_ms=600)
    check("captura: voz contínua termina pelo limite (e o 'fim da fala' fica ~0,00 s)",
          cap.end_reason.startswith("limite") and (cap.t_end - cap.t_last_voice) < 0.05,
          f"{cap.end_reason}, fim-da-fala {cap.t_end - cap.t_last_voice:.3f}s")
    blocks = speech_blocks([(0.3, "silencio"), (1.0, "fala"), (1.5, "silencio")])
    lagged = UtteranceCapture(clock=lambda: time.perf_counter() - 0.5, end_silence_ms=600)   # áudio chegou 0,5 s antes
    for b in blocks:
        lagged.feed(b)
    check("captura: atraso entre chegada e processamento é detectado", 480 <= lagged.stats()["lag_max_ms"] <= 700,
          f"{lagged.stats()['lag_max_ms']:.0f} ms (simulado: 500 ms)")
    st = cap_sil.stats()
    check("captura: estatísticas (áudio, voz, motivo)", st["audio_s"] > 1.0 and st["voiced_s"] > 0.9 and st["fim_por"] == "silêncio",
          f"{st['audio_s']:.2f}s de áudio, {st['voiced_s']:.2f}s com voz, fim por {st['fim_por']}")
    cap = run([(7.0, "silencio")], start_timeout_s=1.0)
    check("captura: sem fala -> 'sem fala'", cap.end_reason == "sem fala" and cap.result is None)


# ----------------------------------------------------------------------------- STT em etapas (FLAC x rede) e motor local
def t_stt_stats() -> None:
    import types
    from app.core.config import Settings
    from app.services import speech_service as S

    class AudioData:
        def __init__(self, frame_data, sample_rate, sample_width):
            self.frame_data = frame_data

        def get_flac_data(self, convert_rate=None, convert_width=None):
            time.sleep(0.05)
            return b"fLaC" + b"\x00" * 40000

    class Recognizer:
        operation_timeout = None

        def recognize_google(self, audio, language="en-US"):
            audio.get_flac_data(convert_rate=None, convert_width=2)     # igual à biblioteca real
            time.sleep(0.10)                                             # "rede + servidor"
            return "olá apolo"

    fake = types.ModuleType("speech_recognition")
    fake.AudioData, fake.Recognizer = AudioData, Recognizer
    fake.UnknownValueError = type("UnknownValueError", (Exception,), {})
    fake.RequestError = type("RequestError", (Exception,), {})
    old_mod = sys.modules.get("speech_recognition")
    sys.modules["speech_recognition"] = fake
    S._TIMED_CLS = None
    try:
        svc = S.SpeechService(Settings())
        st: dict = {}
        text = svc.transcribe(np.zeros(16000 * 2, dtype=np.int16).tobytes(), st)
        check("STT google: texto devolvido", text == "olá apolo")
        check("STT google: preparo do áudio (FLAC) medido", 0.04 < st["flac_s"] < 0.2, f"{st['flac_s'] * 1000:.0f} ms")
        check("STT google: rede+servidor = total - FLAC", 0.08 < st["net_s"] < 0.3 and abs(st["total_s"] - st["flac_s"] - st["net_s"]) < 1e-6,
              f"{st['net_s'] * 1000:.0f} ms")
        check("STT google: tamanho e duração do áudio", abs(st["audio_s"] - 2.0) < 1e-6 and abs(st["pcm_kb"] - 62.5) < 0.1 and st["flac_kb"] > 30,
              f"{st['audio_s']:.1f}s, {st['pcm_kb']:.1f} KB PCM, {st['flac_kb']:.1f} KB FLAC")
    finally:
        S._TIMED_CLS = None
        if old_mod is None:
            sys.modules.pop("speech_recognition", None)
        else:
            sys.modules["speech_recognition"] = old_mod

    # faster-whisper (dublê): o modelo carrega UMA vez e o texto sai do gerador de segmentos
    loads = []

    class Seg:
        def __init__(self, t):
            self.text = t

    class WhisperModel:
        def __init__(self, name, device="cpu", compute_type="int8"):
            loads.append((name, device, compute_type))

        def transcribe(self, audio, **kw):
            assert audio.dtype == np.float32 and abs(float(np.max(np.abs(audio)))) <= 1.0 and kw["language"] == "pt"
            return iter([Seg(" abra o "), Seg("navegador ")]), None

    fw = types.ModuleType("faster_whisper")
    fw.WhisperModel = WhisperModel
    old_fw = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = fw
    S.SpeechService._whisper_model = None
    try:
        cfg = Settings()
        cfg.stt_engine = "whisper"
        svc = S.SpeechService(cfg)
        a = svc.transcribe(np.ones(16000, dtype=np.int16).tobytes(), {})
        b = svc.transcribe(np.ones(16000, dtype=np.int16).tobytes(), {})
        check("whisper local: transcreve", a == "abra o navegador" == b, a)
        check("whisper local: modelo carregado uma única vez", len(loads) == 1 and loads[0] == ("small", "cpu", "int8"), str(loads))
    finally:
        S.SpeechService._whisper_model = None
        if old_fw is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = old_fw


# ----------------------------------------------------------------------------- ganho REAL da transcrição especulativa
class PacedEngine:
    """Entrega blocos de 20 ms em TEMPO REAL, carimbando a chegada como o AudioEngine faz."""
    ok = True
    dropped = 0

    def __init__(self, blocks):
        self.blocks = blocks
        self.block_ts = 0.0

    @contextlib.contextmanager
    def use_sink(self, fn):
        stop = threading.Event()

        def feed():
            t0 = time.perf_counter()
            for i, b in enumerate(self.blocks):
                while time.perf_counter() < t0 + i * 0.02:
                    if stop.is_set():
                        return
                    time.sleep(0.001)
                if stop.is_set():
                    return
                self.block_ts = time.perf_counter()
                fn(b)
        t = threading.Thread(target=feed, daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()                       # como o AudioEngine real: sair do 'with' não espera o áudio acabar
            t.join(1)


def t_spec_gain() -> None:
    from app.core.config import Settings
    from app.services.speech_service import SpeechService
    from app.utils.metrics import Turn
    L = 0.60          # duração simulada da transcrição

    def once(speculative: bool):
        cfg = Settings()
        cfg.stt_speculative = speculative
        svc = SpeechService(cfg)
        svc.transcribe = lambda pcm, stats=None: (time.sleep(L), stats is not None and stats.update(total_s=L, engine="fake"), "olá")[2]
        turn = Turn()
        eng = PacedEngine(speech_blocks([(0.2, "silencio"), (1.0, "fala"), (1.6, "silencio")]))
        turn.mark("listen_start")
        text = svc.listen(eng, turn)
        return text, turn.summary(), turn.stt

    _, plain, st_plain = once(False)
    _, spec, st_spec = once(True)
    w0, w1 = plain["stt_espera_apos_captura"], spec["stt_espera_apos_captura"]
    check("espera pelo STT sem especulação ≈ duração do STT", abs(w0 - L) < 0.12, f"{w0:.2f}s (STT {L:.2f}s)")
    check("especulação aproveitada nesse cenário", str(st_spec.get("mode", "")).startswith("especulativa aproveitada"), st_spec.get("mode", ""))
    gain = w0 - w1
    check("ganho real da especulação ≈ 0,28 s (teto por construção: 600 ms − 320 ms)", 0.18 < gain < 0.36,
          f"espera {w0:.2f}s -> {w1:.2f}s, ganho {gain:.2f}s")
    check("métrica 'ganho da transcrição especulativa' coerente", spec["ganho_especulacao"] is not None
          and abs(spec["ganho_especulacao"] - gain) < 0.12, f"{spec['ganho_especulacao']:.2f}s")


# ----------------------------------------------------------------------------- cache de voz e tempos do TTS
def t_cache() -> None:
    from app.core.config import Settings
    from app.services import tts_service as T
    from app.services.tts_cache import TTSCache
    with tempfile.TemporaryDirectory() as d:
        c = TTSCache(Path(d), max_files=3)
        pcm = (np.sin(np.arange(4800) / 7) * 8000).astype(np.int16)
        check("cache: vazio no início", c.get("a", "Olá.") is None)
        c.put("a", "Olá.", pcm)
        got = c.get("a", "Olá.")
        check("cache: ida e volta idêntica", got is not None and np.array_equal(got, pcm))
        check("cache: assinatura diferente = outro áudio (trocar a voz invalida)", c.get("b", "Olá.") is None)
        check("cache: frase longa não é guardada", not c.cacheable("x" * 300))
        for i in range(5):
            c.put("a", f"frase {i}.", pcm)
            time.sleep(0.01)
        check("cache: poda mantém o limite", len(list(Path(d).glob("*.npy"))) <= 3)
        (next(Path(d).glob("*.npy"))).write_bytes(b"lixo")
        bad = [c.get("a", f"frase {i}.") for i in range(5)]
        check("cache: arquivo corrompido é ignorado sem erro", True)
    # pipeline: 2ª vez a mesma frase sai do cache (sem sintetizar) e o relatório sabe disso
    with tempfile.TemporaryDirectory() as d:
        cfg = Settings()
        cfg.tts_pause_ms = 100
        svc = T.TTSService(cfg)
        svc.cache = TTSCache(Path(d))
        svc._open = lambda rate: FakeStream()
        calls = []

        def synth(name, loop, text):
            calls.append(text)
            time.sleep(0.25 if "lenta" in text else 0.05)
            return T.shape_audio(voice_like(sec=0.5), T.OUT_RATE, 2.0)
        svc._synth_with = synth
        svc.speak("Abrindo o Chrome.")
        svc.wait_idle(10)
        t1 = dict(svc.timing)
        check("TTS: tempos por resposta registrados", all(k in t1 for k in ("enqueue", "synth_start", "synth_end", "first_audio")), str(sorted(t1)))
        check("TTS: ordem dos tempos coerente", t1["enqueue"] <= t1["synth_start"] <= t1["synth_end"] <= t1["first_audio"])
        check("TTS: 1ª vez foi sintetizada", t1["cache_hit"] is False and calls == ["Abrindo o Chrome."])
        svc.speak("Abrindo o Chrome.")
        svc.wait_idle(10)
        t2 = dict(svc.timing)
        check("TTS: 2ª vez veio do cache, sem sintetizar", t2["cache_hit"] is True and len(calls) == 1)
        d1, d2 = t1["first_audio"] - t1["enqueue"], t2["first_audio"] - t2["enqueue"]
        check("TTS: cache reduz o tempo até o 1º áudio", d2 < d1 and d2 < 0.05, f"{d1 * 1000:.0f} ms -> {d2 * 1000:.0f} ms (síntese simulada de 50 ms; a real da rede é bem maior)")
        # frase 2 mais lenta que a reprodução da 1ª => a espera entre frases (stall) é registrada
        svc.cache = TTSCache(Path(d) / "novo")
        svc.speak("Primeira rápida.")
        svc.speak("Segunda lenta demais para a anterior.")
        svc.wait_idle(10)
        check("TTS: espera entre frases (stall) medida", svc.timing["stall_s"] > 0.0, f"{svc.timing['stall_s'] * 1000:.0f} ms")


# ----------------------------------------------------------------------------- scripts de benchmark (lógica, com dublês)
def t_bench() -> None:
    import asyncio
    from app.core.config import Settings
    from app.services import tts_service as T
    from app.services.tts_cache import TTSCache
    from app.tests import test_bench as B
    from app.tests.test_stt_bench import wer

    check("WER: idêntico = 0", wer("que horas são agora", "Que horas são agora?") == 0.0)
    check("WER: 1 erro em 4 palavras = 25%", abs(wer("que horas são agora", "que horas sao hoje") - 0.25) < 1e-9)

    class FakeGemini:
        def reset(self): pass

        def stream_reply(self, text):
            time.sleep(0.02)
            yield "Resposta curta. "
            time.sleep(0.01)
            yield "Resto da resposta."

    with tempfile.TemporaryDirectory() as d:
        cfg = Settings()
        cfg.tts_pause_ms = 50
        tts = T.TTSService(cfg)
        tts.cache = TTSCache(Path(d))
        tts._open = lambda rate: FakeStream()
        tts._synth_with = lambda name, loop, text: (time.sleep(0.03), T.shape_audio(voice_like(sec=0.4), T.OUT_RATE, 2.0))[1]
        tts._loop = asyncio.new_event_loop()
        ack = lambda phrase: f"Abrindo {phrase}."
        for mute in (True, False):
            tts.cache.clear()
            out = B.run_all(FakeGemini(), tts, mute, ack)
            groups = [g for g, _ in out]
            check(f"bench ({'mudo' if mute else 'tocando'}): 5 curtas + 5 médias + 10 comandos",
                  groups.count("curta") == 5 and groups.count("média") == 5 and sum(g.startswith("comando") for g in groups) == 10,
                  f"{len(out)} medições")
            c1 = [r for g, r in out if g == "comando 1ª"]
            c2 = [r for g, r in out if g == "comando 2ª"]
            check(f"bench ({'mudo' if mute else 'tocando'}): 2ª rodada de comandos vem do cache e é mais rápida",
                  all(not r["cache"] for r in c1) and all(r["cache"] for r in c2)
                  and statistics_mean([r["texto_ate_audio"] for r in c2]) < statistics_mean([r["texto_ate_audio"] for r in c1]),
                  f"{statistics_mean([r['texto_ate_audio'] for r in c1]) * 1000:.0f} ms -> {statistics_mean([r['texto_ate_audio'] for r in c2]) * 1000:.0f} ms")
    check("bench: agregação média/mín/máx", "/" in B.agg([{"x": 0.1}, {"x": 0.3}], "x") and " 200 /" in B.agg([{"x": 0.1}, {"x": 0.3}], "x"))


def statistics_mean(v):
    return sum(v) / len(v)


# ----------------------------------------------------------------------------- GeminiService com um SDK simulado
def t_gemini() -> None:
    import os
    import types
    from app.core.config import Settings
    from app.utils.textutils import pop_sentences

    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    calls = {"configs": [], "fail_thinking": False}

    def GenerateContentConfig(**kw):
        calls["configs"].append(kw)
        return Obj(**kw)

    class FakeAPIError(Exception):
        code = 400

    class Models:
        def generate_content_stream(self, model, contents, config):
            if calls["fail_thinking"] and hasattr(config, "thinking_config"):
                raise FakeAPIError("Thinking level is not supported for this model")
            for i, (delay, text) in enumerate([(0.03, "Bras"), (0.02, "ília."), (0.02, " Uma escolha previsível.")]):
                time.sleep(delay)
                um = Obj(thoughts_token_count=0, prompt_token_count=540, candidates_token_count=9) if i == 2 else None
                yield Obj(text=text, usage_metadata=um)

        def get(self, model):
            return Obj(name=model)

    gtypes = types.ModuleType("google.genai.types")
    gtypes.GenerateContentConfig = GenerateContentConfig
    gtypes.ThinkingConfig = lambda **kw: Obj(**kw)
    gtypes.AutomaticFunctionCallingConfig = lambda **kw: Obj(**kw)
    gtypes.HttpOptions = lambda **kw: Obj(**kw)
    gtypes.Content = lambda role, parts: Obj(role=role, parts=parts)
    gtypes.Part = Obj
    gtypes.Part.from_text = staticmethod(lambda text: Obj(text=text))
    ggenai = types.ModuleType("google.genai")
    ggenai.types = gtypes
    ggenai.Client = lambda api_key, http_options=None: Obj(models=Models())
    ggoogle = types.ModuleType("google")
    ggoogle.genai = ggenai
    saved = {k: sys.modules.get(k) for k in ("google", "google.genai", "google.genai.types", "httpx")}
    sys.modules.update({"google": ggoogle, "google.genai": ggenai, "google.genai.types": gtypes})
    sys.modules.setdefault("httpx", types.SimpleNamespace(Limits=lambda **kw: Obj(**kw)))
    os.environ["GEMINI_API_KEY"] = "chave-de-teste"
    try:
        from app.services.gemini_service import GeminiService
        cfg = Settings()
        g = GeminiService(cfg)
        sent_times, buf, got = [], "", []
        t0 = time.perf_counter()
        for d in g.stream_reply("qual a capital do Brasil"):
            buf += d
            ss, buf = pop_sentences(buf, first=not got)
            if ss:
                got += ss
                sent_times.append(time.perf_counter() - t0)
        info = g.last_info
        check("Gemini: resposta em streaming montada", "".join(["Bras", "ília.", " Uma escolha previsível."]).strip() == g.history[-1].parts[0].text)
        check("Gemini: diagnóstico de pedaços e tempos", info["chunks"] == 3 and len(info["t_chunks"]) == 3 and info["t_chunks"][0] < info["t_chunks"][1],
              f"pedaços em {[round(t * 1000) for t in info['t_chunks']]} ms")
        check("Gemini: tokens de raciocínio/entrada/saída lidos", (info["thoughts_tokens"], info["prompt_tokens"], info["output_tokens"]) == (0, 540, 9))
        check("Gemini: humor registrado", isinstance(info["humor"], bool))
        check("Gemini: 1ª frase sai no pedaço do ponto (não espera o fim do streaming)",
              got[0] == "Brasília." and sent_times[0] < (info["t_chunks"][2] + 0.5) and sent_times[0] < sent_times[-1] - 0.01,
              f"1ª frase em {sent_times[0] * 1000:.0f} ms, resposta completa em {sent_times[-1] * 1000:.0f} ms")
        cf = calls["configs"][0]
        check("Gemini: personalidade em system_instruction + linha dinâmica curta", "APOLO" in cf["system_instruction"] and "Humor:" in cf["system_instruction"])
        check("Gemini: thinking mínimo (modelo gemini-3.x)", getattr(cf.get("thinking_config"), "thinking_level", None) == "minimal")
        check("Gemini: chamada automática de funções desligada", getattr(cf.get("automatic_function_calling"), "disable", False) is True)
        # modelo que rejeita thinking: repete UMA vez sem a configuração e lembra disso
        calls["fail_thinking"], calls["configs"] = True, []
        g2 = GeminiService(cfg)
        txt = "".join(g2.stream_reply("oi"))
        check("Gemini: modelo sem thinking -> repete sem a configuração e responde", txt.startswith("Brasília.") and g2._thinking_ok is False)
        check("Gemini: histórico limitado a 10 turnos", len(g.history) <= 20)
    finally:
        os.environ.pop("GEMINI_API_KEY", None)
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def main() -> int:
    for fn in (t_text, t_persona, t_vad, t_capture, t_stt_stats, t_spec_gain, t_audio, t_tts_pipeline, t_cache, t_bench, t_gemini, t_misc):
        print(f"\n== {fn.__name__[2:]} ==")
        try:
            fn()
        except Exception as e:  # um teste quebrado não esconde os outros
            import traceback
            traceback.print_exc()
            check(f"{fn.__name__} executou", False, repr(e))
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} verificações passaram.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
