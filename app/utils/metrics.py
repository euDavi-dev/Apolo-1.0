"""Cronometragem de um turno (ouvir -> entender -> responder -> falar).

Regras:
 - Só se imprime o que foi MEDIDO. Etapa que não ocorreu no turno (ex.: Gemini numa resposta local) não aparece.
 - LATÊNCIA = tempo até o APOLO começar a falar (o que importa). DURAÇÃO = quanto tempo ele fala (não é latência).
 - Os instantes de fala (voice_end etc.) usam a hora em que o microfone ENTREGOU cada bloco de áudio, não a hora em
   que o código o processou; assim, fila/atraso de processamento aparecem em vez de ficarem escondidos.

Marcas (todas time.perf_counter(); só a 1ª ocorrência de cada uma vale):
  listen_start     começou a escutar            speech_start    o VAD detectou o início da fala
  voice_end        último bloco com voz         speech_end      o VAD confirmou o fim (hora do áudio)
  capture_done     o código recebeu a captura   stt_start/done  transcrição
  reply_ready      texto da resposta local      gemini_sent / first_token / gemini_done
  first_sentence   1ª frase entregue ao TTS     tts_first_audio 1º som no alto-falante     playback_end"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("apolo.metrics")
W = 56  # largura interna da caixa


class Turn:
    def __init__(self, label: str = "voz"):
        self.label = label
        self.kind = ""                         # gemini | comando | hora | data | clima | confirmação
        self.t0 = time.perf_counter()
        self.marks: dict[str, float] = {}
        self.stt: dict = {}                    # engine, audio_s, pcm_kb, flac_s, net_s, total_s, mode
        self.cap: dict = {}                    # fim_por, audio_s, voiced_s, lag_max_ms, dropped
        self.tts: dict = {}                    # enqueue, synth_start/end, first_audio, engine, cache_hit, stall_s...
        self.diag: dict = {}                   # cmd_exec_s, weather_s...
        self.wake: dict = {}                   # latency_s, vad_wait_s, stt_s, listen_after_s, confidence (ativação por voz)
        self._lock = threading.Lock()

    def mark(self, name: str, t: float | None = None) -> None:
        with self._lock:
            self.marks.setdefault(name, time.perf_counter() if t is None else t)

    def span(self, a: str, b: str) -> float | None:
        ma, mb = self.marks.get(a), self.marks.get(b)
        return None if ma is None or mb is None else max(0.0, mb - ma)

    def absorb_tts(self, rt: dict | None) -> None:
        """Copia os tempos internos do TTS desta resposta (ignora os de um turno anterior)."""
        if not rt or rt.get("enqueue", 0) < self.t0:
            return
        self.tts = dict(rt)
        for src, dst in (("enqueue", "tts_enqueue"), ("synth_start", "tts_synth_start"),
                         ("synth_end", "tts_synth_end"), ("first_audio", "tts_first_audio")):
            if src in rt:
                self.marks.setdefault(dst, rt[src])

    # ---- números para benchmarks -------------------------------------------------
    def summary(self) -> dict:
        s = self.span
        out = {
            "silencio_ate_fim": s("voice_end", "speech_end"),
            "fim_fala_ate_stt": s("voice_end", "stt_done"),
            "stt": s("stt_start", "stt_done"),
            "stt_espera_apos_captura": s("capture_done", "stt_done"),
            "stt_para_gemini": s("stt_done", "gemini_sent"),
            "gemini_1o_token": s("gemini_sent", "first_token"),
            "gemini_1a_frase": s("first_token", "first_sentence"),
            "tratamento_local": s("stt_done", "reply_ready"),
            "tts_fila": s("tts_enqueue", "tts_synth_start"),
            "tts_sintese": s("tts_synth_start", "tts_synth_end"),
            "tts_ate_audio": s("tts_synth_end", "tts_first_audio"),
            "frase_ate_audio": s("first_sentence", "tts_first_audio"),
            "fim_fala_ate_audio": s("voice_end", "tts_first_audio"),
            "duracao_resposta": s("tts_first_audio", "playback_end"),
            "total": s("voice_end", "playback_end"),
        }
        out["stt_rede"] = self.stt.get("net_s")
        out["stt_preparo"] = self.stt.get("flac_s")
        # STT "completo" = quanto a transcrição levou no relógio; "espera" = quanto você realmente esperou por ela
        # depois que a captura terminou. Com a especulação aproveitada, a diferença é o tempo escondido.
        full, wait = self.stt.get("total_s"), out["stt_espera_apos_captura"]
        out["stt_completo"] = full
        out["ganho_especulacao"] = (max(0.0, full - wait) if full is not None and wait is not None
                                    and str(self.stt.get("mode", "")).startswith("especulativa aproveitada") else None)
        return out

    # ---- relatório -----------------------------------------------------------------------
    def report(self) -> str:
        s = self.summary()
        rows: list[str] = []

        def row(label: str, value: float | None, indent: int = 0) -> None:
            if value is not None:
                rows.append(f"{' ' * indent}{label}".ljust(W - 9) + f"{value:6.2f}s")

        def info(text: str) -> None:
            rows.append(text[:W])

        w = self.wake
        if w:
            row("Wake: fim de \"Apolo\" → detecção", w.get("latency_s"))
            row("espera do VAD pelo fim da frase", w.get("vad_wait_s"), 2)
            row("STT do wake word" + (" (especulativo)" if w.get("spec_used") else ""), w.get("stt_s"), 2)
            row("Wake: detecção → captura do comando", w.get("listen_after_s"))
        row("Silêncio até o VAD confirmar o fim", s["silencio_ate_fim"])
        row("Fim da fala → texto (STT)", s["fim_fala_ate_stt"])
        if s["stt"] is not None:
            row("transcrição (início → texto)", s["stt"], 2)
        row("preparar áudio (FLAC)", s["stt_preparo"], 4)
        row("rede + servidor STT", s["stt_rede"], 4)
        row("espera após a captura terminar", s["stt_espera_apos_captura"], 2)
        row("ganho da transcrição especulativa", s["ganho_especulacao"], 2)
        row("STT → Gemini enviado", s["stt_para_gemini"])
        row("Gemini → 1º token", s["gemini_1o_token"])
        row("1º token → 1ª frase completa", s["gemini_1a_frase"])
        row("Texto → resposta local pronta", s["tratamento_local"])
        row("1ª frase → 1º áudio", s["frase_ate_audio"])
        row("espera na fila do TTS", s["tts_fila"], 2)
        row("síntese da 1ª frase", s["tts_sintese"], 2)
        t_ = self.tts
        row("conexão + 1º pedaço de áudio (edge)", t_.get("edge_first_chunk_s"), 4)
        row("resto do áudio chegando (edge)", t_.get("edge_stream_s"), 4)
        row("decodificar MP3", t_.get("edge_decode_s"), 4)
        row("pós-processar (graves, volume)", t_.get("post_s"), 4)
        row("síntese pronta → som", s["tts_ate_audio"], 2)
        head = ["╔" + "═" * (W + 2) + "╗", "║" + "APOLO LATENCY".center(W + 2) + "║", "╠" + "═" * (W + 2) + "╣"]
        body = ["║ " + r.ljust(W) + " ║" for r in rows]
        foot = ["╟" + "─" * (W + 2) + "╢"]
        if s["fim_fala_ate_audio"] is not None:
            foot.append("║ " + ("FIM DA FALA → PRIMEIRO ÁUDIO".ljust(W - 9) + f"{s['fim_fala_ate_audio']:6.2f}s").ljust(W) + " ║")
        if s["duracao_resposta"] is not None:
            foot.append("║ " + ("DURAÇÃO DA RESPOSTA (não é latência)".ljust(W - 9) + f"{s['duracao_resposta']:6.2f}s").ljust(W) + " ║")
        if s["total"] is not None:
            foot.append("║ " + ("Total (fim da fala → fim da resposta)".ljust(W - 9) + f"{s['total']:6.2f}s").ljust(W) + " ║")
        foot.append("╚" + "═" * (W + 2) + "╝")

        d: list[str] = []
        c, st, t = self.cap, self.stt, self.tts
        if c:
            d.append(f"  captura : {c.get('audio_s', 0):.1f}s de áudio ({c.get('voiced_s', 0):.1f}s com voz), "
                     f"terminou por {c.get('fim_por', '?')}, início da fala {s_fmt(self.span('listen_start', 'speech_start'))}")
            d.append(f"  mic     : atraso máx. de processamento {c.get('lag_max_ms', 0):.0f} ms, "
                     f"blocos perdidos {c.get('dropped', 0)}")
        if st:
            bits = [f"motor {st.get('engine', '?')}", st.get("mode", "")]
            if st.get("pcm_kb") is not None:
                bits.append(f"enviado {st['pcm_kb']:.0f} KB PCM" + (f" → {st['flac_kb']:.0f} KB FLAC" if st.get("flac_kb") else ""))
            d.append("  STT     : " + ", ".join(b for b in bits if b))
        g = self.diag.get("gemini")
        if g:
            tc = g.get("t_chunks") or []
            d.append(f"  Gemini  : {g.get('chunks', 0)} pedaços (1º {s_fmt(tc[0] if tc else None)}, 2º {s_fmt(tc[1] if len(tc) > 1 else None)}), "
                     f"raciocínio {g.get('thoughts_tokens') if g.get('thoughts_tokens') is not None else 'n/d'} tokens, "
                     f"humor {'permitido' if g.get('humor') else 'não'}")
        if self.kind:
            d.append(f"  resposta: {self.kind}" + (f" (comando {self.diag['cmd_exec_s']:.2f}s)" if "cmd_exec_s" in self.diag else "")
                     + (f" (clima {self.diag['weather_s']:.2f}s)" if "weather_s" in self.diag else ""))
        if t:
            d.append(f"  TTS     : motor {t.get('engine', '?')}, {'do CACHE' if t.get('cache_hit') else 'sintetizado'}"
                     + (f", saída de áudio aberta em {t['stream_open_s']:.2f}s" if t.get("stream_open_s") else "")
                     + (f", esperas entre frases {t['stall_s']:.2f}s" if t.get("stall_s") else ""))
        title = f"Latência do turno ({self.label})"
        return "\n".join([title] + head + body + foot + d)

    def log(self) -> None:
        log.info("\n" + self.report())


def s_fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}s"
