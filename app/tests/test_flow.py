"""python -m app.tests.test_flow  — fluxo completo do Assistant com dublês (sem microfone, internet ou caixa de som).

Valida a fiação: sessão -> STT -> comandos/Gemini -> TTS frase a frase -> estados -> métricas.
Usa o PySide6 real se estiver instalado; caso contrário, um stub mínimo de QObject/Signal."""
from __future__ import annotations

import logging
import sys
import time
import types

try:
    import PySide6.QtCore  # noqa: F401
except Exception:  # ambiente sem Qt: stub mínimo só para importar app.core.assistant
    qt = types.ModuleType("PySide6")
    core = types.ModuleType("PySide6.QtCore")

    class _Sig:
        def __init__(self):
            self.calls = []

        def emit(self, *a):
            self.calls.append(a)

        def connect(self, *a):
            pass

    class QObject:
        def __init__(self, *a, **k):
            for name in dir(type(self)):
                if isinstance(getattr(type(self), name, None), _Sig):
                    setattr(self, name, _Sig())

    class Signal:
        def __init__(self, *a, **k):
            pass

        def __set_name__(self, owner, name):
            pass

        def __get__(self, obj, owner):
            return self

    # Signal como descritor que cria um _Sig por instância
    class _SignalDescriptor:
        def __init__(self, *a, **k):
            self.name = None

        def __set_name__(self, owner, name):
            self.name = "_sig_" + name

        def __get__(self, obj, owner):
            if obj is None:
                return self
            if self.name not in obj.__dict__:
                obj.__dict__[self.name] = _Sig()
            return obj.__dict__[self.name]

    core.QObject, core.Signal = QObject, _SignalDescriptor
    sys.modules["PySide6"], sys.modules["PySide6.QtCore"] = qt, core

from app.core.assistant import Assistant  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.state import State  # noqa: E402
from app.services import command_service  # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"[{'OK' if cond else 'FALHOU'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILS.append(name)


class FakeAudio:
    ok = True
    device = None
    _running = True

    def start(self): return True
    def stop(self): pass
    def restart(self): return True
    def flush(self): pass
    block_ts = 0.0

    def set_sink(self, fn, keep_backlog=False): pass


class FakeTTS:
    def __init__(self):
        self.spoken, self.on_first_audio, self.engine = [], None, "edge"
        self.timing: dict = {}

    def speak(self, text):
        if not self.spoken:
            self.timing = {"enqueue": time.perf_counter(), "engine": "edge", "cache_hit": False}
            if self.on_first_audio:
                self.on_first_audio(time.perf_counter())
        self.spoken.append(text)

    def prerender(self, *a, **k): pass
    def play_pcm(self, pcm, gap_ms=0): pass

    def wait_idle(self, timeout=None): pass
    def stop(self): pass
    def start(self): pass
    def label(self): return "NEURAL"


def build(heard, chunks):
    a = Assistant(Settings())
    states, msgs = [], []
    a.state_changed.connect(states.append)
    # com Qt real os sinais precisam de slots; com o stub, lemos .calls
    a.audio, a.tts = FakeAudio(), FakeTTS()
    a.gemini.prewarm = lambda: None
    a.gemini.stream_reply = lambda text: iter(chunks)
    def fake_listen(eng, turn=None, on_speech_end=None, seed=None, activation=False, on_ready=None):
        if on_ready:
            on_ready()
        t = time.perf_counter()                       # tempos fictícios, relativos a "agora"
        if turn:
            for k, dt in (("speech_start", -2.0), ("voice_end", -0.6), ("speech_end", 0.0),
                          ("stt_start", -0.28), ("stt_done", 0.2)):
                turn.mark(k, t + dt)
        if on_speech_end:
            on_speech_end()
        return heard
    a.speech.listen = fake_listen
    return a, states


def run_session(a):
    assert a._lock.acquire(blocking=False)
    a._session("voice", "clap", "")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def main() -> int:
    import app.core.assistant as asm
    asm.play_activation_sound = lambda *_: None
    cap = Capture()
    logging.getLogger("apolo.metrics").addHandler(cap)
    logging.getLogger("apolo.metrics").setLevel(logging.INFO)

    # 1) pergunta ao Gemini, resposta em duas frases
    a, _ = build("qual a capital do Brasil", ["Brasília. ", "Uma escolha bastante ", "previsível."])
    run_session(a)
    states = [c[0] for c in getattr(a.state_changed, "calls", [])]
    if states:
        check("estados: ACTIVATED > LISTENING > PROCESSING > SPEAKING > IDLE",
              [s for i, s in enumerate(states) if i == 0 or s != states[i - 1]] ==
              ["ACTIVATED", "LISTENING", "PROCESSING", "SPEAKING", "IDLE"], " > ".join(states))
    check("TTS recebeu a resposta frase a frase", a.tts.spoken == ["Brasília.", "Uma escolha bastante previsível."],
          str(a.tts.spoken))
    report = "\n".join(cap.lines)
    check("métricas registradas no log (caixa APOLO LATENCY)", "APOLO LATENCY" in report and "FIM DA FALA → PRIMEIRO ÁUDIO" in report
          and "Gemini → 1º token" in report, "")
    print(report)

    # 2) comando local (não chama o Gemini)
    popen_calls = []
    command_service.subprocess.Popen = lambda *a_, **k: popen_calls.append(a_)
    a, _ = build("abra o bloco de notas", ["não deveria ser usado"])
    run_session(a)
    check("comando executado sem passar pelo Gemini", len(popen_calls) == 1 and "usado" not in " ".join(a.tts.spoken))
    check("confirmação de comando falada", len(a.tts.spoken) == 1 and "bloco de notas" in a.tts.spoken[0].lower(), str(a.tts.spoken))

    # 2b) resposta local LONGA é falada frase a frase; CURTA sai inteira (dividir criaria um buraco entre as partes)
    a, _ = build("clima", ["x"])
    a._begin_turn("voz")
    a._speak("Em Salvador faz 27 graus, com céu limpo e poucas nuvens. A umidade do ar é de 80 por cento. "
             "O vento sopra a 15 quilômetros por hora e não há previsão de chuva.")
    check("resposta local longa dividida em frases para o TTS", len(a.tts.spoken) == 3, str(a.tts.spoken))
    check("marca 'primeira frase' registrada nas respostas locais", "first_sentence" in a._turn.marks)
    a, _ = build("x", ["x"])
    a._begin_turn("voz")
    a._speak("Pronto. Abrindo o Chrome.")
    check("confirmação curta vai em um único pedido", a.tts.spoken == ["Pronto. Abrindo o Chrome."], str(a.tts.spoken))

    # 3) hora local
    a, _ = build("que horas são", ["x"])
    run_session(a)
    check("hora respondida localmente", a.tts.spoken and any(k in a.tts.spoken[0] for k in ("hora", "meia-noite", "meio-dia")), str(a.tts.spoken))

    # 4) erro do Gemini mantém personalidade, mas o erro real é levantado/logado
    from app.services.gemini_service import GeminiError
    def boom(text):
        raise GeminiError("network", "Não consegui falar com o serviço de IA.")
        yield
    a, _ = build("qualquer coisa", [])
    a.gemini.stream_reply = boom
    run_session(a)
    check("erro do Gemini falado de forma amigável", any("serviço de IA" in t for t in a.tts.spoken), str(a.tts.spoken))

    # 5) silêncio: sem resposta e sem travar
    a, _ = build(None, [])
    run_session(a)
    check("silêncio encerra a sessão sem falar", a.tts.spoken == [] and not a._lock.locked())
    check("trava liberada ao fim da sessão", not a._lock.locked())

    print(f"\n{'Tudo certo.' if not FAILS else str(len(FAILS)) + ' falha(s): ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
