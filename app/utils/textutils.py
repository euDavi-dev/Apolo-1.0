from __future__ import annotations

import re
import unicodedata
from datetime import datetime

WEEKDAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
          "setembro", "outubro", "novembro", "dezembro"]
MONTHS_SHORT = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]
WEEKDAYS_SHORT = ["SEG", "TER", "QUA", "QUI", "SEX", "SÁB", "DOM"]


def normalize(text: str) -> str:
    """minúsculas, sem acentos, sem pontuação — para comparar comandos falados."""
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return t.strip()


def spoken_time(dt: datetime) -> str:
    h, m = dt.hour, dt.minute
    if h == 0:
        hs = "meia-noite"
    elif h == 12:
        hs = "meio-dia"
    elif h == 1:
        hs = "uma hora"
    elif h == 2:
        hs = "duas horas"
    else:
        hs = f"{h} horas"
    verb = "É" if h in (0, 1, 12) else "São"
    ms = ""
    if m == 1:
        ms = " e um minuto"
    elif m == 2:
        ms = " e dois minutos"
    elif m > 2:
        ms = f" e {m} minutos"
    return f"{verb} {hs}{ms}."


def spoken_date(dt: datetime) -> str:
    return f"Hoje é {WEEKDAYS[dt.weekday()]}, {dt.day} de {MONTHS[dt.month - 1]} de {dt.year}."


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002190-\U000021FF\U00002300-\U000023FF\U00002600-\U000027BF"
                    "\U00002B00-\U00002BFF\U0001F1E6-\U0001F1FF\uFE0F\u200d]+")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://)?[^)\s]*\)")
_REAIS = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})*|\d+)(?:,(\d{1,2}))?")
_HM = re.compile(r"\b([01]?\d|2[0-3])h([0-5]\d)\b")
_H = re.compile(r"\b([01]?\d|2[0-3])h\b")
_CLOCK = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


def _hours(h: str) -> str:
    return "1 hora" if int(h) == 1 else f"{int(h)} horas"


def _reais(m: re.Match) -> str:
    whole = int(m.group(1).replace(".", ""))
    out = f"{whole} {'real' if whole == 1 else 'reais'}"
    if m.group(2):
        cents = int(m.group(2).ljust(2, "0"))
        if cents:
            out += f" e {cents} {'centavo' if cents == 1 else 'centavos'}"
    return out


def clean_for_display(text: str) -> str:
    """Texto para a bolha de chat: tira markdown/emoji/links, mantém números e símbolos como escritos."""
    t = _EMOJI.sub("", text)
    t = _MD_LINK.sub(r"\1", t)
    t = re.sub(r"[*_`#>~]+", "", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)
    return re.sub(r"\s+", " ", t).strip()


def clean_for_tts(text: str) -> str:
    """Deixa o texto natural para ser PRONUNCIADO: sem markdown/emoji/símbolos estranhos para o TTS."""
    t = _EMOJI.sub("", text)
    t = _MD_LINK.sub(r"\1", t)
    t = re.sub(r"https?://\S+", "link", t)
    t = re.sub(r"^\s*(?:[-•*]|\d+[.)])\s+", "", t, flags=re.M)      # marcadores de lista
    t = re.sub(r"(?<![.!?…:;,])[ \t]*\n+[ \t]*", ". ", t)               # quebra de linha sem pontuação vira pausa
    t = re.sub(r"[*_`#>~]+", "", t)
    t = _REAIS.sub(_reais, t)
    t = _HM.sub(lambda m: f"{_hours(m.group(1))} e {int(m.group(2))} minutos", t)
    t = _CLOCK.sub(lambda m: _hours(m.group(1)) + (f" e {int(m.group(2))} minutos" if m.group(2) != "00" else ""), t)
    t = _H.sub(lambda m: _hours(m.group(1)), t)
    t = re.sub(r"\bkm/h\b", "quilômetros por hora", t)
    t = re.sub(r"(\d)\s*[°º]\s*C\b", r"\1 graus Celsius", t)
    t = re.sub(r"(\d)\s*[°º]\s*F\b", r"\1 graus Fahrenheit", t)
    t = re.sub(r"(\d)\s*[°º]", r"\1 graus", t)
    t = re.sub(r"(\d)\s*%", r"\1 por cento", t)
    t = re.sub(r"\s&\s", " e ", t)
    t = re.sub(r"\s\+\s", " mais ", t)
    t = re.sub(r"\s*[—–]\s*", ", ", t)                                # travessão vira pausa
    t = re.sub(r"\s*\(([^)]*)\)", r", \1,", t)                         # (aparte) vira aparte entre vírgulas
    t = re.sub(r"\be/ou\b", "e ou", t)
    t = re.sub(r"(?<=\w)/(?=\w)", " ou ", t)
    t = re.sub(r",\s*([.!?…])", r"\1", t)
    t = re.sub(r",(\s*,)+", ",", t)
    t = re.sub(r"([!?.])\1{2,}", r"\1", t) if "..." not in t else re.sub(r"([!?])\1+", r"\1", t)
    return re.sub(r"\s+", " ", t).strip(" ,")


_BOUNDARY = re.compile(r"[.!?…]+\s+|\n+")
_ABBR = {"sr", "sra", "srta", "dr", "dra", "prof", "profa", "av", "ex", "etc", "obs", "vs", "pag", "ref", "min", "max"}


def _is_boundary(buf: str, m: re.Match) -> bool:
    if "\n" in m.group(0):
        return True
    punct = m.group(0).strip()
    if punct != ".":
        return True
    before = buf[:m.start()]
    word = re.search(r"([^\W\d_]+)$", before)
    if word and (word.group(1).lower() in _ABBR or (len(word.group(1)) == 1 and word.group(1).isupper())):
        return False
    if re.search(r"\d$", before) and re.match(r"\s*\d", buf[m.end():] or "x"):
        return False                                                  # item numerado ou decimal quebrado
    nxt = buf[m.end():m.end() + 1]
    return not (nxt and nxt.islower())                                # "etc. e tal": continua a mesma frase


def _terminal_is_safe(t: str) -> bool:
    """O buffer termina numa pontuação que, com certeza razoável, encerra a frase?

    '?', '!' e '…' sempre. '.' só depois de uma palavra comum: não depois de número ("3." pode virar "3.5"),
    de abreviação ("Dr.") nem de inicial ("J.")."""
    if len(t) < 3:
        return False
    c = t[-1]
    if c in "!?…":
        return True
    if c != ".":
        return False
    before = t[:-1]
    m = re.search(r"([^\W\d_]+)$", before)
    if not m:
        return False                                                  # número, '..' ou símbolo antes do ponto
    w = m.group(1)
    return not (w.lower() in _ABBR or (len(w) == 1 and w.isupper()))


def pop_sentences(buf: str, first: bool = False) -> tuple[list[str], str]:
    """Separa frases completas de um buffer de texto em streaming; devolve (frases, resto).

    `first=True` (ainda nada foi falado nesta resposta) aceita cortar na 1ª vírgula quando a frase está longa,
    para a voz começar mais cedo."""
    out, start = [], 0
    for m in _BOUNDARY.finditer(buf):
        if _is_boundary(buf, m):
            piece = buf[start:m.end()].strip()
            if piece:
                out.append(piece)
            start = m.end()
    rest = buf[start:]
    tail = rest.strip()
    if tail and _terminal_is_safe(tail):
        # A frase já está completa mesmo sem um espaço depois do ponto (ex.: a resposta curta "Brasília.").
        # Esperar o próximo trecho do streaming (ou o fim dele) atrasava o início da fala.
        out.append(tail)
        rest = ""
    if out:
        return out, rest
    limit = (60, 28) if first else (160, 40)                          # (tamanho mínimo do buffer, posição mínima da vírgula)
    if len(rest) > limit[0]:
        i = rest.find(", ", limit[1]) if first else rest.rfind(", ")
        if i > limit[1] - 1:
            return [rest[: i + 1]], rest[i + 2:]
    return [], rest
