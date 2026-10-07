"""python -m app.tests.test_activation  — testes da ativação por voz ("Apolo") + palmas. Sem microfone, internet ou modelo.

Usa dublês para o STT (um transcritor que "ouve" o texto combinado), o TTS e o microfone; o resto é o código real:
VAD (UtteranceCapture), WakeWordDetector, Assistant.activate_apolo, sessão, estados, respostas pré-renderizadas.

  python -m app.tests.test_activation                    # tudo
  python -m app.tests.test_activation --require-assets   # falha se os 10 WAVs reais ainda não foram gerados
  python -m app.tests.test_activation --live             # MICROFONE + faster-whisper reais: mostra o que ouve e ativações
  python -m app.tests.test_activation --benchmark        # mede N ativações em tempo real: média, mín, máx, P95 (--n 20 --stt-ms 150)
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from app.tests import test_flow as tf                       # (instala o stub do Qt se o PySide6 não existir)
from app.tests.test_clap import synth                       # noqa: E402
from app.tests.test_offline import speech_blocks            # noqa: E402

import app.core.assistant as asm                            # noqa: E402
from app.core.config import ACTIVATION_DIR, ACTIVATION_PHRASES, Settings  # noqa: E402
from app.core.state import State                            # noqa: E402
from app.services.activation_audio import ActivationAudio, clip_filename, generate  # noqa: E402
from app.services.audio_engine import BLOCK                 # noqa: E402
from app.services.wake_word import Heard, WakeWordDetector, match_wake, wake_confidence  # noqa: E402

FAILS: list[str] = []
PENDING: list[str] = []
check = tf.check


def fail_hook() -> None:
    FAILS[:] = tf.FAILS


# ------------------------------------------------------------------------------------------- dublês
class RecAudio(tf.FakeAudio):
    def __init__(self):
        self.sink = None

    def set_sink(self, fn, keep_backlog=False):
        self.sink = fn


class TTS(tf.FakeTTS):
    busy = False

    def __init__(self):
        super().__init__()
        self.played: list[int] = []

    def play_pcm(self, pcm, gap_ms=0):
        self.played.append(len(pcm))

    def voice_signature(self, engine):
        return f"{engine}|fake"


class Hearing:
    """Transcritor falso: devolve o texto combinado e conta as chamadas (= quanto STT foi gasto)."""

    def __init__(self, text="", delay=0.0):
        self.text, self.delay, self.calls = text, delay, 0

    def __call__(self, pcm: bytes) -> str:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return self.text


def make(heard="qual a capital do Brasil", chunks=("Brasília.",), text="Apolo"):
    asm.play_activation_sound = lambda *_: TONES.append(1)
    a, _ = tf.build(heard, list(chunks))
    a.audio, a.tts = RecAudio(), TTS()
    a.cfg.wake_word_enabled = a.cfg.clap_enabled = True
    # Estes cenários verificam a resposta falada e a captura por fim de frase.
    # O caminho antecipado e o sinal curto têm testes próprios.
    a.cfg.activation_feedback = "voice"
    a.cfg.wake_prefix_s = 0
    a.wake.ready = True
    a.wake.transcribe = ear = Hearing(text)
    calls, orig = [], a.activate_apolo

    def wrapped(source="voice", command="", info=None):
        calls.append((source, command))
        return orig(source, command, info)

    a.activate_apolo = wrapped
    listens = []
    real_listen = a.speech.listen
    a.speech.listen = lambda *x, **k: (listens.append(1), real_listen(*x, **k))[1]
    return a, ear, calls, listens


TONES: list[int] = []


def wait_idle(a, timeout=6.0) -> bool:
    t0 = time.time()
    time.sleep(0.05)
    while a._lock.locked() and time.time() - t0 < timeout:
        time.sleep(0.02)
    return not a._lock.locked()


def say(a, segments):
    """Alimenta o sink de standby do Assistant como o microfone faria (blocos de 20 ms)."""
    for b in speech_blocks(segments):
        a._standby_sink(b)
    a.wake.flush()


def utterance(speech_s=0.6):
    return [(1.0, "silencio"), (speech_s, "fala"), (1.0, "silencio")]      # 1 s inicial > guarda de 0,8 s


def load_clips(a, tmp: Path):
    class G:
        def engines(self): return ["edge"]
        def synthesize(self, text, engine):
            return (np.full(2400 + 100 * len(text), 800, dtype=np.int16), 24000, 0.0)
        def voice_signature(self, engine): return "edge|fake"
    generate(G(), ACTIVATION_PHRASES, tmp, "edge|fake")
    a.activation = ActivationAudio(tmp, ACTIVATION_PHRASES)
    return a.activation.load()


# ------------------------------------------------------------------------------------------- testes
def t_match() -> None:
    for t in ("Apolo", "apolo", "APOLO", "Apolo", "Apolo!", "Apolo?", "Apolo.", "Apollo", "Apólo", "A polo", "ei Apolo", "Bom dia, Apolo"):
        check(f"match: {t!r} é a palavra de ativação", match_wake(t).found)
    for t in ("Jarbas", "Travis", "Carlos", "eu gosto do Apolo", "o Apolo é legal", "obrigado", "", "qual é a temperatura"):
        check(f"match: {t!r} NÃO ativa", not match_wake(t).found)
    m = match_wake("Apolo, qual é o tempo?")
    check("match: 'Apolo, qual é o tempo?' -> comando extraído", m.found and m.command == "qual é o tempo?", repr(m.command))
    check("match: só 'Apolo' -> sem comando", match_wake("Apolo.").command == "")
    check("match: 'Apolo, Apolo' = só chamou", match_wake("Apolo, Apolo").command == "")
    check("match: cortesia + comando", match_wake("ei Apolo, abra o Chrome").command == "abra o Chrome")
    check("match: acentos preservados no comando", match_wake("Apolo: que horas são?").command == "que horas são?")


def t_wake_only() -> None:
    a, ear, calls, listens = make(heard="qual a capital do Brasil")
    with tempfile.TemporaryDirectory() as d:
        n = load_clips(a, Path(d))
        a._resume_listening()
        say(a, utterance())
        ok = wait_idle(a)
    check("1) 'Apolo' ativa", calls == [("voice", "")], str(calls))
    check("1) sessão concluída e comando seguinte respondido", ok and a.tts.spoken == ["Brasília."], str(a.tts.spoken))
    check("1) chamada por voz usa sinal curto sem fala sobre o pedido", a.tts.played == [2160] and TONES == [], f"{a.tts.played}")
    check("1) depois da resposta, escuta o comando (1 escuta)", len(listens) == 1)
    check("10) volta a STANDBY: estado IDLE, microfone devolvido ao sink de standby, wake desmutado",
          a.state == State.IDLE and a.audio.sink == a._standby_sink and not a.wake.muted,
          f"{a.state.value}, muted={a.wake.muted}")
    check("10) sessão liberou a trava", not a._lock.locked())
    return calls


def t_command_inline() -> None:
    TONES.clear()
    a, ear, calls, listens = make(heard="NÃO DEVERIA OUVIR", text="Apolo, que horas são?")
    with tempfile.TemporaryDirectory() as d:
        load_clips(a, Path(d))
        a._resume_listening()
        say(a, utterance(1.4))
        ok = wait_idle(a)
    check("2) 'Apolo, <comando>' ativa e extrai o comando", calls == [("voice", "que horas são?")], str(calls))
    check("2) executa direto, sem pedir para repetir (nenhuma escuta extra)", ok and len(listens) == 0, f"escutas={len(listens)}")
    check("2) sem resposta de ativação antes (nem tons)", a.tts.played == [] and TONES == [])
    check("2) comando respondido (hora local)", a.tts.spoken and ("hora" in a.tts.spoken[0] or "meio-dia" in a.tts.spoken[0]
                                                                  or "meia-noite" in a.tts.spoken[0]), str(a.tts.spoken))


def t_single_trigger() -> None:
    a, ear, calls, _ = make(text="Apolo")
    a.cfg.wake_soft_silence_ms = 200
    with tempfile.TemporaryDirectory() as d:
        load_clips(a, Path(d))
        a._resume_listening()
        say(a, utterance())
        wait_idle(a)
    check("3) uma fala de 'Apolo' = UMA ativação", len(calls) == 1 and a.wake.stats["activations"] == 1, str(calls))
    check("3) um único job de STT por frase (especulativa reaproveitada)", ear.calls == 1, f"{ear.calls} chamadas")
    # a mesma frase de novo, imediatamente (eco/duplicata): debounce do detector
    det = WakeWordDetector(Settings(), Hearing("Apolo"), lambda c, h, i=None: calls.append(("dup", c)) or True)
    det.ready = True
    t0 = len(calls)
    det._handle("Apolo")
    det._handle("Apolo")
    check("3) mesmo áudio/texto duas vezes seguidas: debounce bloqueia a 2ª", len(calls) - t0 == 1 and det.stats["debounced"] == 1)


def t_no_self_trigger() -> None:
    check("4) nenhuma frase de ativação contém a palavra 'Apolo' (a resposta não se reativa)",
          all(not match_wake(p).found for p in ACTIVATION_PHRASES))
    a, ear, calls, _ = make(text="Apolo")
    a._resume_listening()
    a.tts.busy = True                                          # APOLO falando
    say(a, utterance())
    check("4) TTS falando: microfone ignorado (nem STT é gasto)", calls == [] and ear.calls == 0, f"stt={ear.calls}")
    a.tts.busy = False
    a.wake._guard = 0
    a.wake.muted = True                                        # sessão em andamento
    say(a, utterance())
    check("4) sessão em andamento: microfone ignorado", calls == [] and ear.calls == 0)
    a.wake.muted = False
    a.wake.reset(guard_s=0.8)                                  # cauda do áudio logo após falar
    for b in speech_blocks([(0.6, "fala")]):
        a._standby_sink(b)
    a.wake.flush()
    check("4) guarda pós-fala: eco logo depois do TTS é ignorado", calls == [] and ear.calls == 0)
    a.wake.reset(guard_s=0.0)
    say(a, utterance())
    wait_idle(a)
    check("4) passada a guarda, voltar a ouvir 'Apolo' funciona", len(calls) == 1, str(calls))
    # silêncio nunca gasta STT
    a2, ear2, calls2, _ = make(text="Apolo")
    a2._resume_listening()
    say(a2, [(3.0, "silencio")])
    check("   VAD: só silêncio => STT nunca é chamado", ear2.calls == 0)


def t_cooldown() -> None:
    a, ear, calls, _ = make()
    first = a.activate_apolo("voice")
    wait_idle(a)
    second = a.activate_apolo("voice")                       # logo após terminar: dentro do cooldown
    clap2 = a.activate_apolo("clap")
    check("5) 1ª ativação aceita", first is True)
    check("5) cooldown: 2ª ativação (voz) e 3ª (palmas) logo em seguida são ignoradas", second is False and clap2 is False)
    ui = a.activate_apolo("ui")                              # botão da tela não sofre cooldown
    wait_idle(a)
    check("5) botão 'Falar' da interface não é barrado pelo cooldown", ui is True)
    a._last_activation -= a.cfg.activation_cooldown_s + 0.1
    third = a.activate_apolo("voice")
    wait_idle(a)
    check("5) passado o cooldown, ativa de novo", third is True)
    a._lock.acquire()
    check("5) sessão em andamento: ativação simultânea é ignorada", a.activate_apolo("ui") is False)
    a._lock.release()


def t_clap_and_sources() -> None:
    TONES.clear()
    a, ear, calls, listens = make(heard="que dia é hoje", text="ruído")
    with tempfile.TemporaryDirectory() as d:
        load_clips(a, Path(d))
        a._resume_listening()
        for i in range(0, len(x := synth([(1.0, .05, 1.2, "clap"), (1.4, .05, 1.2, "clap")], 4.0)) - BLOCK + 1, BLOCK):
            a._standby_sink(x[i:i + BLOCK])
        ok = wait_idle(a)
    check("6) duas palmas continuam ativando", calls == [("clap", "")] and ok, str(calls))
    check("6) palmas também tocam a resposta de ativação e depois escutam o comando",
          len(a.tts.played) == 1 and len(listens) == 1, f"{a.tts.played}, escutas={len(listens)}")
    a2, _, calls2, _ = make(text="Apolo")
    a2.cfg.clap_enabled = False
    a2._resume_listening()
    for i in range(0, len(x) - BLOCK + 1, BLOCK):
        a2._standby_sink(x[i:i + BLOCK])
    check("6) com palmas desligadas nas Configurações, palmas não ativam", calls2 == [])
    return calls


def t_both_sources(voice_calls, clap_calls) -> None:
    srcs = {c[0] for c in voice_calls} | {c[0] for c in clap_calls}
    check("7) palmas e voz chamam o MESMO activate_apolo(source)", srcs == {"voice", "clap"}, str(srcs))


def t_assets() -> None:
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        class G:
            def __init__(self, bad=()): self.bad, self.used = set(bad), []
            def engines(self): return ["edge", "outro"]
            def synthesize(self, text, engine):
                self.used.append(engine)
                if engine in self.bad:
                    raise RuntimeError("sem internet")
                return (np.full(3000, 700, dtype=np.int16), 24000, 0.0)
            def voice_signature(self, engine): return f"{engine}|v"

        g = G()
        m = generate(g, ACTIVATION_PHRASES, d, "edge|v")
        names = sorted(p.name for p in d.glob("*.wav"))
        check("8) gerador cria os 10 WAVs", len(names) == 10 == len(ACTIVATION_PHRASES), f"{len(names)}")
        check("8) nomes no padrão pedido (01_estou_aqui.wav, 02_a_sua_disposicao.wav, 03_pois_nao.wav, 04_sim_senhor.wav)",
              names[:4] == ["01_estou_aqui.wav", "02_a_sua_disposicao.wav", "03_pois_nao.wav", "04_sim_senhor.wav"], str(names[:4]))
        check("8) usa o motor/voz configurados (edge) e registra o manifest", m["engine"] == "edge" and set(g.used) == {"edge"})
        g2 = G(bad={"edge"})
        m2 = generate(g2, ACTIVATION_PHRASES, d, "edge|v")
        check("8) edge indisponível: todas as frases saem do mesmo motor de reserva (não mistura vozes)",
              m2["engine"] == "outro" and g2.used.count("outro") == 10)
        store = ActivationAudio(d, ACTIVATION_PHRASES)
        check("8) needs_generation=False quando em dia", not store.needs_generation("edge|v"))
        check("8) needs_generation=True se a voz configurada mudou", store.needs_generation("edge|OUTRA"))
        (d / names[3]).unlink()
        check("8) needs_generation=True se faltar arquivo", store.needs_generation("edge|v") and len(store.missing()) == 1)
        generate(G(), ACTIVATION_PHRASES, d, "edge|v")
        # 9) pré-carregamento
        t0 = time.perf_counter()
        n = store.load()
        dt = (time.perf_counter() - t0) * 1000
        check("9) os 10 áudios carregam na inicialização", n == 10 and all(c.pcm.dtype == np.int16 for c in store.clips), f"{dt:.1f} ms")
        for f in d.glob("*"):
            f.unlink()
        picks = [store.pick().name for _ in range(40)]
        check("9) depois de carregados, tocar NÃO lê o disco (arquivos apagados, pick() segue funcionando)",
              len(picks) == 40 and not list(d.glob("*.wav")))
        check("9) escolha aleatória sem repetir a anterior em sequência", all(a != b for a, b in zip(picks, picks[1:])) and len(set(picks)) > 3)
    real = ActivationAudio(ACTIVATION_DIR, ACTIVATION_PHRASES)
    miss = real.missing()
    if not miss:
        check("8) os 10 arquivos REAIS existem em assets/sounds/activation", True)
    else:
        PENDING.append(f"{len(miss)} WAVs reais ainda não gerados")
        print(f"[PENDENTE] 8) arquivos reais em assets/sounds/activation: faltam {len(miss)}/10 — rode "
              f"python scripts/generate_activation_audio.py (precisa de internet para o edge-tts)")
        if "--require-assets" in sys.argv:
            check("8) os 10 arquivos REAIS existem", False, f"faltam {len(miss)}")


def t_latency() -> None:
    """Tempo (do fim da fala até on_wake) com e sem transcrição especulativa; STT falso de 150 ms, microfone em tempo real."""
    def run(soft_ms):
        cfg = Settings()
        cfg.wake_soft_silence_ms, cfg.wake_end_silence_ms = soft_ms, 450
        cfg.wake_low_latency = soft_ms > 0
        hit = {}
        det = WakeWordDetector(cfg, Hearing("Apolo", delay=0.15), lambda c, h, i=None: hit.setdefault("t", time.perf_counter()) or True)
        det.ready = True
        for b in speech_blocks([(0.3, "silencio"), (0.6, "fala")]):
            det.feed(b)
            time.sleep(0.02)
        t_end = time.perf_counter()
        for b in speech_blocks([(1.0, "silencio")]):
            det.feed(b)
            time.sleep(0.02)
            if "t" in hit:
                break
        det.flush()
        return hit.get("t", t_end + 9) - t_end
    base, spec = run(0), run(200)
    print(f"      latência fim-da-fala -> ativação: sem especulação {base * 1000:.0f} ms · com {spec * 1000:.0f} ms (STT falso 150 ms)")
    check("latência: a transcrição especulativa antecipa a ativação", spec < base - 0.05, f"{spec:.2f}s vs {base:.2f}s")



def t_confidence() -> None:
    """Pontuação/confiança: frases pedidas na especificação."""
    cfg = Settings()
    def decide(text, dec=0.8, voiced=0.6):
        m = match_wake(text)
        return m.found and wake_confidence(m, dec, voiced) >= cfg.wake_min_confidence
    for t in ("Apolo", "Apolo", "Apollo", "Hey Apolo", "ei Apolo", "Apolo, que horas são?", "Apolo, toque Believer"):
        check(f"confiança: {t!r} ativa", decide(t))
    for t in ("Jarbas", "Travis", "Elvis", "Charles", "Carlos", "gosto do Apolo", "eu estava falando sobre o Apolo ontem",
              "que horas são", "bom dia a todos", "toque Believer", ""):
        check(f"confiança: {t!r} NÃO ativa", not decide(t))
    m = match_wake("Apolo")
    check("confiança: 'Apolo' nítido tem confiança alta (>= 0.85)", wake_confidence(m, 0.8, 0.6) >= 0.85, f"{wake_confidence(m, .8, .6):.2f}")
    check("confiança: 'ei apolo' também é alta (>= 0.80)", wake_confidence(match_wake("ei apolo"), 0.8, 0.6) >= 0.80)
    check("confiança: variação duvidosa + decodificador fraco é rejeitada", not decide("Apollo", dec=0.05))
    check("confiança: o mesmo 'Apollo' com decodificador firme é aceito", decide("Apollo", dec=0.7))
    check("confiança: 'Apolo' sozinho em 4 s de voz (alucinação?) é rejeitado", not decide("Apolo", dec=0.8, voiced=4.0))
    check("confiança: 'Apolo, <comando>' em fala longa continua válido", decide("Apolo, abra o navegador", dec=0.8, voiced=4.0))
    # no detector: texto ruim do decodificador é barrado e contado
    hits = []
    det = WakeWordDetector(Settings(), Hearing(""), lambda c, h, i=None: hits.append(c) or True)
    det.ready = True
    from app.services.wake_word import WakeInfo
    det._handle(Heard("Apollo", 0.05), WakeInfo(voiced_s=0.5))
    check("detector: confiança baixa não ativa (e é contada)", hits == [] and det.stats["low_conf"] == 1)


def t_carry_over() -> None:
    """Ponta a ponta com o AudioEngine REAL (sem placa de som): a pessoa diz "Apolo", faz uma pausa e já fala o comando
    enquanto o STT do wake word ainda está rodando. O comando inteiro tem de chegar ao STT; nada de "Estou aqui"."""
    import threading
    from app.services.audio_engine import AudioEngine
    from app.services.speech_service import SpeechService

    class Eng(AudioEngine):
        ok = True

    TONES.clear()
    a, ear, calls, listens = make(heard="NÃO USADO", text="Apolo")
    ear.delay = 0.6                                           # STT lento: o resultado chega com o comando já em andamento
    eng = Eng()
    eng._running = True
    threading.Thread(target=eng._consume, args=(eng._gen,), daemon=True).start()
    a.audio = eng
    a.speech.listen = SpeechService.listen.__get__(a.speech)  # captura REAL (com seed + backlog)
    got = []
    a.speech.transcribe = lambda pcm, stats=None: (got.append(len(pcm)), "que horas são")[1]
    with tempfile.TemporaryDirectory() as d:
        load_clips(a, Path(d))
        a._resume_listening()
        plan = [(1.0, "silencio"), (0.6, "fala"), (0.55, "silencio"), (1.2, "fala"), (1.2, "silencio")]
        t0 = time.perf_counter()
        for i, b in enumerate(speech_blocks(plan)):
            eng._q.put_nowait((time.perf_counter(), b))
            time.sleep(max(0.0, t0 + (i + 1) * 0.02 - time.perf_counter()))
        wait_idle(a, timeout=8)
    check("carry-over: uma ativação por voz", len(calls) == 1 and calls[0][0] == "voice", str(calls))
    check("carry-over: SEM resposta 'Estou aqui' (o comando já estava em andamento)", a.tts.played == [] and TONES == [])
    check("carry-over: o STT do comando recebeu a fala inteira (>= 1,2 s de áudio)", bool(got) and got[0] / 32000 >= 1.2,
          f"{(got[0] / 32000) if got else 0:.2f}s")
    check("carry-over: comando executado (hora local)", bool(a.tts.spoken) and any(k in a.tts.spoken[0] for k in ("hora", "meio-dia", "meia-noite")),
          str(a.tts.spoken))
    check("carry-over: ao fim voltou ao standby", a.state == State.IDLE and a.audio._sink == a._standby_sink)
    w = a.wake.history[-1] if a.wake.history else None
    check("carry-over: latência fim-da-fala → detecção foi medida", w is not None and w.latency_s is not None,
          f"{(w.latency_s * 1000) if w and w.latency_s else 0:.0f} ms")
    eng._running = False


def t_adaptive_guard() -> None:
    """Guarda de eco: termina cedo se o microfone já está no nível ambiente; dura mais se ainda há cauda de áudio."""
    def blocks_until_open(low_latency, tail_blocks):
        cfg = Settings()
        cfg.wake_low_latency = low_latency
        det = WakeWordDetector(cfg, Hearing(""), lambda *a: True)
        det.ready = True
        det.reset(guard_s=0.8)
        n = 0
        tone = (0.15 * np.sin(np.arange(BLOCK) * 0.3)).astype(np.float32)
        quiet = (np.random.default_rng(0).standard_normal(BLOCK) * 0.002).astype(np.float32)
        while det._guard_left > 0 and n < 200:
            det.feed(tone if n < tail_blocks else quiet)
            n += 1
        return n
    quiet = blocks_until_open(True, 0)
    tail = blocks_until_open(True, 25)
    econ = blocks_until_open(False, 0)
    print(f"      guarda de eco: sala quieta {quiet * 20} ms · com cauda de 500 ms {tail * 20} ms · modo econômico {econ * 20} ms (teto 800 ms)")
    check("guarda adaptativa: sala quieta libera o microfone cedo (< 400 ms)", quiet * 20 < 400, f"{quiet * 20} ms")
    check("guarda adaptativa: com cauda de áudio espera a cauda acabar (> 500 ms)", tail * 20 > 500, f"{tail * 20} ms")
    check("guarda adaptativa: nunca passa do teto de 800 ms", tail * 20 <= 800)
    check("guarda: modo econômico usa o tempo cheio (800 ms)", econ * 20 == 800, f"{econ * 20} ms")


def t_music_mode() -> None:
    cfg = Settings()
    ear = Hearing("Apolo")
    det = WakeWordDetector(cfg, ear, lambda *a: True)
    # Tons sintéticos testam a política de duração, não a classificação do WebRTC.
    # O VAD agressivo pode rejeitar senoides sustentadas como música (corretamente).
    new_capture = det._new_capture
    def energy_capture():
        cap = new_capture()
        cap.vad = None
        return cap
    det._new_capture = energy_capture
    det.ready = True
    det.noisy = lambda: True
    for b in speech_blocks([(1.0, "silencio"), (4.5, "fala"), (1.0, "silencio")]):
        det.feed(b)
    det.flush()
    check("música tocando: voz longa (letra cantada) nem vai ao STT", ear.calls == 0 and det.stats["music_skipped"] == 1)
    for b in speech_blocks([(0.3, "silencio"), (0.7, "fala"), (1.0, "silencio")]):
        det.feed(b)
    det.flush()
    check("música tocando: frase curta ('Apolo') continua sendo processada", ear.calls == 1)


def t_buffers() -> None:
    """Pré/pós-buffer configuráveis e coerentes com a especulação."""
    from app.services.speech_service import UtteranceCapture
    c = UtteranceCapture(pre_ms=300, post_ms=200, soft_silence_ms=200, end_silence_ms=450)
    check("buffer pré-wake: 300 ms = 15 quadros guardados", c.pre.maxlen == 15)
    c2 = UtteranceCapture(pre_ms=300, post_ms=600, soft_silence_ms=200, end_silence_ms=450)
    check("buffer pós-wake nunca excede o silêncio soft (senão a especulativa seria sempre descartada)", c2.post_frames == 10)
    cap = UtteranceCapture(pre_ms=300, post_ms=200, soft_silence_ms=0, end_silence_ms=450)
    for b in speech_blocks([(0.5, "silencio"), (0.6, "fala"), (0.6, "silencio")]):
        cap.feed(b)
    dur = len(cap.result) / 32000
    check("clip enviado ao STT = pré-roll + fala + pós-roll (não o silêncio inteiro)", 0.6 < dur < 1.5, f"{dur:.2f}s")


def t_seed_listen() -> None:
    """SpeechService.listen(seed): o áudio do seed entra ANTES do backlog do motor, em ordem."""
    import threading
    from app.services.audio_engine import AudioEngine
    from app.services.speech_service import SpeechService
    class Eng(AudioEngine):
        ok = True
    eng = Eng()
    eng._running = True
    threading.Thread(target=eng._consume, args=(eng._gen,), daemon=True).start()
    svc = SpeechService(Settings())
    got = []
    svc.transcribe = lambda pcm, stats=None: (got.append(pcm), "ok")[1]
    cmd = speech_blocks([(1.0, "fala"), (1.0, "silencio")])
    seed = (np.concatenate(cmd[:20]) * 32767).astype(np.int16).tobytes()      # primeiros 400 ms do comando
    eng.set_sink(None, keep_backlog=True)                                       # "ativação": corte limpo
    for b in cmd[20:35]:                                                        # chega durante a troca -> backlog
        eng._q.put_nowait((time.perf_counter(), b))
    time.sleep(0.15)
    def rest():
        time.sleep(0.2)
        for b in cmd[35:]:
            eng._q.put_nowait((time.perf_counter(), b))
            time.sleep(0.005)
    threading.Thread(target=rest, daemon=True).start()
    text = svc.listen(eng, seed=seed)
    secs = len(got[0]) / 32000 if got else 0
    check("listen(seed): comando completo = seed + backlog + áudio novo (>= 1,0 s de fala)", text == "ok" and secs >= 1.0, f"{secs:.2f}s")
    eng._running = False


# ------------------------------------------------------------------------------------------- benchmark
def _stats(v):
    v = sorted(v)
    if not v:
        return "sem amostras"
    p95 = v[min(len(v) - 1, int(round(0.95 * (len(v) - 1))))]
    return f"média {sum(v) / len(v) * 1000:7.1f} ms · mín {v[0] * 1000:6.1f} · máx {v[-1] * 1000:6.1f} · P95 {p95 * 1000:6.1f}  (n={len(v)})"


def benchmark(n: int, stt_ms: int) -> int:
    """Mede, em TEMPO REAL, o caminho microfone→VAD→detecção com blocos sintéticos e um STT SIMULADO de `stt_ms`.
    NÃO mede o modelo Whisper real nem o seu microfone: isso é o --live. Só imprime o que foi medido."""
    print(f"BENCHMARK  n={n}  STT simulado={stt_ms} ms  (áudio sintético, VAD {'webrtcvad' if _has_webrtc() else 'energia (plano B)'})\n")

    def run(label, **cfgkw):
        lat, vad, stt = [], [], []
        for _ in range(n):
            cfg = Settings()
            for k, v in cfgkw.items():
                setattr(cfg, k, v)
            got = []
            det = WakeWordDetector(cfg, Hearing("Apolo", delay=stt_ms / 1000), lambda c, h, i=None: got.append(i) or True)
            det.ready = True
            t0 = time.perf_counter()
            for i, b in enumerate(speech_blocks([(0.3, "silencio"), (0.6, "fala"), (1.0, "silencio")])):
                det.feed(b)
                time.sleep(max(0.0, t0 + (i + 1) * 0.02 - time.perf_counter()))
                if got:
                    break
            det.flush()
            if got and got[0].latency_s is not None:
                lat.append(got[0].latency_s)
                vad.append(got[0].vad_wait_s or 0)
                stt.append(got[0].stt_s or 0)
        print(f"{label}\n   fim da fala → detecção : {_stats(lat)}\n   espera do VAD          : {_stats(vad)}\n   STT (simulado)         : {_stats(stt)}\n")
        return lat

    a = run("A) baixa latência (padrão): especulativo, fim=450 ms", wake_low_latency=True, wake_end_silence_ms=450, wake_soft_silence_ms=200)
    b = run("B) baixa latência agressivo: especulativo, fim=350 ms", wake_low_latency=True, wake_end_silence_ms=350, wake_soft_silence_ms=160)
    c = run("C) econômico: sem especulação, fim=450 ms", wake_low_latency=False, wake_end_silence_ms=450)

    # custo por bloco no thread do microfone (só o VAD; sem STT)
    cfg = Settings()
    det = WakeWordDetector(cfg, Hearing(""), lambda *a: True)
    det.ready = True
    blocks = speech_blocks([(5.0, "silencio"), (5.0, "fala")])
    t = []
    for blk in blocks:
        t0 = time.perf_counter()
        det.feed(blk)
        t.append(time.perf_counter() - t0)
    t.sort()
    print(f"Custo de feed() por bloco de 20 ms: média {sum(t) / len(t) * 1e6:.0f} µs · P95 {t[int(.95 * len(t))] * 1e6:.0f} µs "
          f"(orçamento: 20.000 µs por bloco)\n")

    # sessão: detecção → modo comando (código do Assistant, TTS/microfone falsos)
    TONES.clear()
    lat2 = []
    for _ in range(max(3, n // 2)):
        asst, ear, calls, listens = make(text="Apolo")
        ear.delay = stt_ms / 1000
        with tempfile.TemporaryDirectory() as d:
            load_clips(asst, Path(d))
            asst._resume_listening()
            say(asst, utterance())
            wait_idle(asst)
        if asst.wake.history and asst.wake.history[-1].listen_after_s is not None:
            lat2.append(asst.wake.history[-1].listen_after_s)
    print(f"Sessão: detecção → captura do comando começou (com resposta pré-gravada, TTS FALSO sem duração real):\n   {_stats(lat2)}\n")

    # modelo real, se existir
    try:
        import faster_whisper  # noqa: F401
        from app.services.wake_word import WhisperWakeSTT
        cfg = Settings.load()
        stt_real = WhisperWakeSTT(cfg)
        if stt_real.load():
            for _ in range(n):
                stt_real(np.zeros(int(RATE_ * 1.0), dtype=np.int16).tobytes())
            print(f"Whisper REAL '{cfg.wake_model}': inferência de 1 s de áudio: {_stats(stt_real.infer_times)}  (carga {stt_real.load_s:.1f}s)")
        else:
            print(f"Whisper REAL indisponível: {stt_real.error}")
    except ImportError:
        print("faster-whisper NÃO está instalado neste ambiente: o tempo real de inferência do modelo NÃO foi medido "
              "(rode este benchmark no seu PC, ou use --live).")
    return 0


def _has_webrtc() -> bool:
    from app.services import speech_service
    return speech_service.webrtcvad is not None


RATE_ = 16000

# ------------------------------------------------------------------------------------------- ao vivo
def live() -> int:
    import logging
    from app.core.config import Settings as S
    from app.services.audio_engine import AudioEngine
    from app.services.wake_word import WhisperWakeSTT
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = S.load()
    stt = WhisperWakeSTT(cfg)
    print(f"Carregando o modelo '{cfg.wake_model}' (1ª vez baixa da internet)...")
    if not stt.load():
        print(stt.error)
        return 1

    def on_wake(cmd, heard, info=None):
        lat = f"{info.latency_s * 1000:.0f} ms" if info and info.latency_s is not None else "?"
        print(f'\n>>> ATIVOU  ouvido={heard!r}  comando={cmd!r}  confiança={info.confidence:.2f}  latência fim-da-fala→detecção={lat}')
        return True

    class Spy:
        def __call__(self, pcm):
            t0 = time.perf_counter()
            h = stt(pcm)
            print(f"   ouvi: {h.text!r}  decodificador={h.confidence:.2f}  ({(time.perf_counter() - t0) * 1000:.0f} ms de STT "
                  f"para {len(pcm) / 32000:.1f}s de áudio)")
            return h

    det = WakeWordDetector(cfg, Spy(), on_wake)
    det.ready = True
    eng = AudioEngine(cfg.input_device)
    if not eng.start():
        print("Microfone indisponível.")
        return 1
    eng.set_sink(det.feed)
    print('Fale "Apolo" ou "Apolo, que horas são?". Ctrl+C para sair.')
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        eng.stop()
    return 0


def main() -> int:
    if "--live" in sys.argv:
        return live()
    if "--benchmark" in sys.argv:
        def arg(name, default):
            return int(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default
        return benchmark(arg("--n", 10), arg("--stt-ms", 150))
    t_match()
    voice_calls = t_wake_only()
    t_command_inline()
    t_single_trigger()
    t_no_self_trigger()
    t_cooldown()
    clap_calls = t_clap_and_sources()
    t_both_sources(voice_calls, clap_calls)
    t_assets()
    t_latency()
    t_confidence()
    t_buffers()
    t_seed_listen()
    t_carry_over()
    t_adaptive_guard()
    t_music_mode()
    fail_hook()
    total = len(tf_results())
    print(f"\n{'Tudo certo.' if not FAILS else str(len(FAILS)) + ' falha(s): ' + ', '.join(FAILS)}"
          + (f"  Pendente: {'; '.join(PENDING)}" if PENDING else ""))
    return 1 if FAILS else 0


def tf_results():
    return tf.FAILS


if __name__ == "__main__":
    sys.exit(main())
