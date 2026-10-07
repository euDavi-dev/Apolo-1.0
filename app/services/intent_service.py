"""Intenções locais (resposta instantânea, sem chamar a IA): hora, data e clima."""
from __future__ import annotations

import re

from app.utils.textutils import normalize

_PATTERNS = {
    "time": r"\bque horas\b|\bhoras sao\b|\b(diga|fale|fala|diz) (as )?horas\b|\bhorario atual\b|\bhora atual\b|\bque hora e\b",
    "date": r"\bque dia e hoje\b|\bdata de hoje\b|\bqual e a data\b|\bdia da semana\b|\bem que dia estamos\b|\bque data\b",
    "weather": r"\b(clima|temperatura|previsao do tempo|vai chover|chuva|esta chovendo|como esta o tempo|tempo hoje|quantos graus)\b",
}


def detect(text: str) -> str | None:
    t = normalize(text)
    for kind, pat in _PATTERNS.items():
        if re.search(pat, t):
            return kind
    return None
