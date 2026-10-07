"""Cliente do Google Gemini (SDK oficial google-genai), com streaming e histórico curto.

Latência:
 - Um único Client (conexão HTTP reaproveitada) e keep-alive longo: o httpx fecha conexões ociosas em ~5 s,
   o que faria a 1ª pergunta depois de cada palma pagar de novo o TLS. `prewarm()` reabre a conexão em
   segundo plano assim que as palmas disparam (enquanto você ainda está falando).
 - thinking mínimo (respostas faladas não precisam de raciocínio longo).
 - A personalidade vai em `system_instruction` (fixa, fora do histórico); só uma linha curta muda por chamada."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Iterator

from app.core.config import Settings, api_key
from app.core.persona import SYSTEM_PROMPT, Persona
from app.utils.textutils import WEEKDAYS, MONTHS

log = logging.getLogger("apolo.gemini")
MAX_TURNS = 10  # pares pergunta/resposta mantidos como contexto
KEEPALIVE_S = 300


class GeminiError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def _now_text() -> str:
    n = datetime.now()
    return f"{WEEKDAYS[n.weekday()]}, {n.day} de {MONTHS[n.month - 1]} de {n.year}, {n:%H:%M}"


class GeminiService:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self._client = None
        self._client_lock = threading.Lock()
        self._key = ""
        self.history: list = []
        self.persona = Persona(cfg.persona_humor)
        self.last_first_token_s: float | None = None
        self.last_total_s: float | None = None
        self.last_info: dict = {}           # diagnóstico da última resposta: pedaços, tempos, tokens, humor
        self._humor_now = False
        self._last_warm = 0.0
        self._thinking_ok = True            # vira False se o modelo rejeitar a configuração de thinking

    # ---- cliente -------------------------------------------------------
    def _get_client(self):
        with self._client_lock:
            return self._get_client_unlocked()

    def _get_client_unlocked(self):
        key = api_key()
        if not key:
            raise GeminiError("nokey", "Não encontrei a chave GEMINI_API_KEY. "
                                       "Abra Minha conta e adicione sua chave Gemini.")
        if self._client is None or key != self._key:
            from google import genai
            from google.genai import types
            try:  # conexão que não esfria entre uma palma e outra (depende da versão do SDK)
                import httpx
                opts = types.HttpOptions(timeout=20000,
                                         client_args={"limits": httpx.Limits(keepalive_expiry=KEEPALIVE_S)})
                self._client = genai.Client(api_key=key, http_options=opts)
            except Exception as e:
                log.info("SDK sem suporte a client_args (%r); usando cliente padrão", e)
                self._client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=20000))
            self._key = key
        return self._client

    def reset(self) -> None:
        self.history.clear()

    def check(self) -> None:
        """Valida chave, conexão e modelo com uma chamada leve (também aquece a conexão TLS)."""
        client = self._get_client()
        try:
            client.models.get(model=self.cfg.gemini_model)
            self._last_warm = time.monotonic()
        except Exception as e:
            raise self._wrap(e) from e

    def prewarm(self) -> None:
        """Reabre a conexão em segundo plano (chamado quando as palmas disparam). Nunca levanta erro."""
        if time.monotonic() - self._last_warm < 4.0:
            return
        self._last_warm = time.monotonic()

        def run():
            try:
                self._get_client().models.get(model=self.cfg.gemini_model)
            except Exception as e:
                log.debug("prewarm do Gemini falhou: %r", e)

        threading.Thread(target=run, daemon=True, name="gemini-prewarm").start()

    # ---- conversa ------------------------------------------------------
    def _thinking_config(self):
        mode = (self.cfg.gemini_thinking or "").lower()
        if not self._thinking_ok or mode not in ("minimal", "low", "off"):
            return None
        from google.genai import types
        model = self.cfg.gemini_model.lower()
        try:
            if model.startswith("gemini-3"):
                return types.ThinkingConfig(thinking_level="low" if mode == "low" else "minimal")
            if model.startswith("gemini-2.5"):
                return types.ThinkingConfig(thinking_budget=0 if mode != "low" else 512)
        except Exception as e:
            log.info("ThinkingConfig indisponível no SDK instalado: %r", e)
        return None

    def _config(self, user_text: str, with_thinking: bool = True):
        from google.genai import types
        self.persona.humor = self.cfg.persona_humor
        self._humor_now = self.persona.allow_humor(user_text)
        dynamic = self.persona.dynamic_line(_now_text(), self._humor_now)
        kwargs = dict(system_instruction=f"{SYSTEM_PROMPT}\n\n{dynamic}",
                      temperature=self.cfg.gemini_temperature,
                      max_output_tokens=self.cfg.gemini_max_tokens)
        thinking = self._thinking_config() if with_thinking else None
        if thinking is not None:
            kwargs["thinking_config"] = thinking
        try:   # sem ferramentas não há o que chamar: desliga o caminho de "chamada automática de funções" do SDK
            return types.GenerateContentConfig(
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True), **kwargs)
        except Exception as e:
            log.debug("SDK sem AutomaticFunctionCallingConfig (%r)", e)
            return types.GenerateContentConfig(**kwargs)

    def stream_reply(self, text: str) -> Iterator[str]:
        from google.genai import types
        client = self._get_client()
        user = types.Content(role="user", parts=[types.Part.from_text(text=text)])
        parts: list[str] = []
        config = self._config(text)
        t0 = time.perf_counter()
        self.last_first_token_s = self.last_total_s = None
        info = self.last_info = {"humor": self._humor_now, "chunks": 0, "t_chunks": [], "thoughts_tokens": None,
                                 "prompt_tokens": None, "output_tokens": None}
        for attempt in (1, 2):
            try:
                for chunk in client.models.generate_content_stream(
                        model=self.cfg.gemini_model, contents=self.history + [user], config=config):
                    um = getattr(chunk, "usage_metadata", None)
                    if um is not None:                       # o último pedaço traz o uso de tokens
                        info["thoughts_tokens"] = getattr(um, "thoughts_token_count", None)
                        info["prompt_tokens"] = getattr(um, "prompt_token_count", None)
                        info["output_tokens"] = getattr(um, "candidates_token_count", None)
                    t = chunk.text
                    if t:
                        now = time.perf_counter() - t0
                        info["chunks"] += 1
                        if len(info["t_chunks"]) < 4:
                            info["t_chunks"].append(now)     # quando chegaram os primeiros pedaços de texto
                        if self.last_first_token_s is None:
                            self.last_first_token_s = now
                        parts.append(t)
                        yield t
                break
            except GeminiError:
                raise
            except Exception as e:
                code = getattr(e, "code", None)
                # modelo que não aceita a configuração de thinking: tenta de novo sem ela (uma única vez)
                if attempt == 1 and not parts and self._thinking_ok and code == 400 and "think" in str(e).lower():
                    log.warning("Modelo rejeitou a configuração de thinking (%r); repetindo sem ela", e)
                    self._thinking_ok = False
                    config = self._config(text, with_thinking=False)
                    continue
                raise self._wrap(e) from e
        self.last_total_s = time.perf_counter() - t0
        answer = "".join(parts).strip()
        if answer:
            self.persona.note_reply(answer)
            model = types.Content(role="model", parts=[types.Part.from_text(text=answer)])
            self.history = (self.history + [user, model])[-2 * MAX_TURNS:]

    def transcribe(self, wav_bytes: bytes, language: str = "pt-BR") -> str:
        """STT alternativo: envia o áudio da frase ao Gemini."""
        from google.genai import types
        client = self._get_client()
        try:
            r = client.models.generate_content(
                model=self.cfg.gemini_model,
                contents=[types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
                          f"Transcreva exatamente o que foi dito ({language}). Responda apenas com a transcrição."],
                config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=200))
            return (r.text or "").strip()
        except Exception as e:
            raise self._wrap(e) from e

    # ---- erros ---------------------------------------------------------
    def _wrap(self, e: Exception) -> GeminiError:
        # o erro REAL sempre vai para o log; a mensagem abaixo é só o que o APOLO diz/mostra
        log.warning("Erro do Gemini: %r", e, exc_info=True)
        code = getattr(e, "code", None)
        text = str(e).lower()
        name = type(e).__name__
        if "api key" in text or "api_key" in text or code in (401, 403):
            return GeminiError("auth", "Minha chave de acesso ao Gemini foi recusada. Confira a chave em Minha conta.")
        if code == 404:
            return GeminiError("model", f"O modelo {self.cfg.gemini_model} não foi encontrado. "
                                        "Altere o modelo em Configurações.")
        if code == 429:
            return GeminiError("quota", "Atingi o limite de uso da API do Gemini. Tente de novo em instantes.")
        if isinstance(code, int) and code >= 500:
            return GeminiError("server", "O serviço de IA está indisponível no momento. Tente novamente em instantes.")
        if isinstance(e, (OSError, TimeoutError)) or any(
                k in name for k in ("Connect", "Timeout", "Network", "Transport", "Read")):
            return GeminiError("network", "Não consegui falar com o serviço de IA.\n"
                                          "A internet parece ter tirado uma folga; verifique a conexão.")
        return GeminiError("unknown", self._unexpected())

    @staticmethod
    def _unexpected() -> str:
        from app.core.persona import shared
        return shared.unexpected()
