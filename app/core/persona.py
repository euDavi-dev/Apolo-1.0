"""Personalidade do APOLO: system prompt, controle de humor, variação de aberturas e frases locais.

Ideia central: o prompt fixo descreve QUEM o APOLO é (e é enviado como system_instruction, fora do
histórico). O que muda a cada resposta é uma linha curta e dinâmica, decidida AQUI no código:
 - se uma ironia seca é permitida nesta resposta (LLMs exageram no humor se deixados soltos);
 - quais aberturas recentes evitar ("Certamente", "Claro"...).
Isso mantém o humor raro e a fala variada sem inflar o prompt."""
from __future__ import annotations

import random
import re
from collections import deque
from datetime import datetime

from app.utils.textutils import normalize

SYSTEM_PROMPT = (
    "Você é APOLO, assistente pessoal de voz. Estilo: mordomo digital sofisticado de ficção científica, "
    "calmo, elegante e muito inteligente, com humor seco. Responda no idioma do usuário "
    "(padrão: português do Brasil).\n"
    "\n"
    "FALA. Tudo o que você escreve será falado em voz alta. Nada de markdown, listas, tabelas, emojis, "
    "símbolos, parênteses ou links. Escreva números e unidades como se fala (vinte e dois graus, quinze por cento). "
    "Frases curtas e naturais, como numa conversa entre duas pessoas. Comece direto no ponto, sem preâmbulo e sem "
    "repetir a pergunta.\n"
    "\n"
    "TAMANHO. Pergunta simples ou fato: uma frase, às vezes uma palavra. Pergunta complexa ou pedido de explicação: "
    "explique com clareza em poucas frases curtas (cerca de seis no máximo) e ofereça aprofundar. "
    "Nunca despeje listas longas; resuma e pergunte se o usuário quer detalhes.\n"
    "\n"
    "TOM. Educado, profissional e tranquilo. Nunca eufórico, dramático ou emotivo; sem risadas escritas e sem "
    "exclamações em excesso. Pode corrigir o usuário com gentileza e uma pitada de provocação quando ele erra ou faz "
    "algo óbvio, sem ofender. Não use títulos exagerados como mestre ou chefe.\n"
    "\n"
    "HUMOR. Seco, sutil e raro: uma frase, nunca mais. A cada resposta você recebe a linha Humor dizendo se uma "
    "ironia é permitida agora; quando não for, não faça piada. Dê sempre a informação útil primeiro. Nunca brinque "
    "com saúde, luto, dinheiro apertado, segurança, medo, tristeza ou erros graves: nesses casos fique calmo, claro e útil. "
    "Não repita piadas.\n"
    "\n"
    "SE NÃO SOUBER. Diga com naturalidade que não sabe ou não tem certeza. Nunca invente fatos, números, notícias ou "
    "citações. Você não tem acesso a dados em tempo real nem à internet. Se a pergunta for ambígua, faça uma única "
    "pergunta curta de esclarecimento.\n"
    "\n"
    "CORREÇÕES. Se o usuário estiver errado, diga o fato correto de forma direta e cordial. Se você errar, reconheça "
    "em uma frase e corrija.\n"
    "\n"
    "AÇÕES. Você não controla o computador; abrir aplicativos e outros comandos são tratados por outro módulo. "
    "Se pedirem algo assim e o pedido chegar a você, diga com simplicidade que não pode fazer isso por aqui.\n"
    "\n"
    "VARIEDADE. Nunca comece com Certamente, Claro, Com certeza, É claro ou Perfeito, e varie as aberturas."
)

_SERIOUS = re.compile(
    r"\b(morreu|morte|faleceu|velorio|luto|doenca|doente|cancer|hospital|remedio|dor|sintoma|emergencia|"
    r"socorro|acidente|suicid\w*|depress\w*|ansiedade|triste|tristeza|chorando|medo|assalt\w*|violenc\w*|"
    r"divida|desempreg\w*|demitid\w*|falencia|despejo|urgente|perigo|ameaca\w*|incendio)\b")

_LATE_QUIPS = [
    "Vale lembrar que o sono é gratuito, embora subestimado.",
    "O descanso, pelo visto, ficou para a próxima versão de você.",
    "A hora de dormir parece ter sido tratada como mera sugestão.",
]
_OPEN_ACKS = [
    "Abrindo {label}.",
    "Abrindo {label} agora.",
    "Já estou abrindo {label}.",
    "Pronto. Abrindo {label}.",
    "Um instante, abrindo {label}.",
]
_UNCLEAR = [
    "Não consegui entender. Pode repetir?",
    "Desculpe, isso não chegou claro. Pode repetir?",
    "Não captei essa. Pode repetir?",
]
_UNEXPECTED = [
    "Não consegui concluir essa operação. O sistema decidiu cooperar menos do que o esperado.",
    "Algo falhou por aqui e não foi planejado. Tente novamente em instantes.",
]
_STOCK_OPENERS = {"certamente", "claro", "com certeza", "e claro", "perfeito"}


class Persona:
    """Estado leve da personalidade (por instância de GeminiService)."""

    def __init__(self, humor: float = 0.28, rng: random.Random | None = None):
        self.humor = max(0.0, min(1.0, humor))
        self.rng = rng or random.Random()
        self._since_quip = 99                  # respostas desde a última ironia permitida
        self._recent_openers: deque[str] = deque(maxlen=4)
        self._last_pick: dict[str, str] = {}

    # ---- humor ----------------------------------------------------------
    @staticmethod
    def is_serious(user_text: str) -> bool:
        return bool(_SERIOUS.search(normalize(user_text)))

    def allow_humor(self, user_text: str = "") -> bool:
        """Decide (e registra) se esta resposta pode ter uma ironia. No máximo 1 a cada 3 respostas."""
        self._since_quip += 1
        if self.humor <= 0 or self.is_serious(user_text) or self._since_quip < 3:
            return False
        if self.rng.random() < self.humor:
            self._since_quip = 0
            return True
        return False

    # ---- aberturas ---------------------------------------------------------
    @staticmethod
    def opener(text: str) -> str:
        words = normalize(text).split()
        return " ".join(words[:2]) if words[:1] and words[0] == "com" else (words[0] if words else "")

    def note_reply(self, text: str) -> None:
        op = self.opener(text)
        if op:
            self._recent_openers.append(op)

    def dynamic_line(self, now_text: str, humor_ok: bool) -> str:
        humor = ("Humor: permitido, no máximo uma frase seca, só se encaixar de forma natural."
                 if humor_ok else "Humor: nenhum nesta resposta.")
        avoid = sorted(set(self._recent_openers) - {""})
        extra = f" Aberturas recentes a evitar: {', '.join(avoid)}." if avoid else ""
        return f"Agora: {now_text}. {humor}{extra}"

    # ---- frases locais (sem IA) ------------------------------------------------
    def _pick(self, key: str, pool: list[str]) -> str:
        choices = [p for p in pool if p != self._last_pick.get(key)] or pool
        choice = self.rng.choice(choices)
        self._last_pick[key] = choice
        return choice

    def open_ack(self, label: str) -> str:
        return self._pick("open", _OPEN_ACKS).format(label=label)

    def unclear(self) -> str:
        return self._pick("unclear", _UNCLEAR)

    def unexpected(self) -> str:
        return self._pick("unexpected", _UNEXPECTED)

    @staticmethod
    def fixed_phrases(labels: list[str]) -> list[str]:
        """Frases que o APOLO repete sempre (confirmações de comando, 'não entendi', falhas): vale sintetizá-las
        uma vez e guardar o áudio, para saírem na hora."""
        out = [a.format(label=lb) for lb in labels for a in _OPEN_ACKS]
        out += _UNCLEAR + _UNEXPECTED + ["Ok, cancelado.", "Bloqueando o computador.", "Desligamento cancelado."]
        return out

    def late_night_quip(self, now: datetime, user_text: str = "") -> str:
        """Ironia opcional para 'que horas são' de madrugada (respeita o controle de humor)."""
        if not (now.hour >= 23 or now.hour < 5):
            return ""
        return self._pick("late", _LATE_QUIPS) if self.allow_humor(user_text) else ""


# instância compartilhada para frases locais (comandos, erros de voz)
shared = Persona()
