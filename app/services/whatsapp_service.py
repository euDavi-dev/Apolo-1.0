"""Abre o WhatsApp e prepara conversas pelo recurso oficial clique para conversa.

O envio continua no WhatsApp, após a revisão da mensagem pelo usuário.
"""
from __future__ import annotations

import re
import unicodedata
import webbrowser
from urllib.parse import quote


class WhatsAppError(ValueError):
    pass


def chat_url(phone: str, message: str = "") -> str:
    phone = phone.strip()
    if not re.fullmatch(r"\+?[0-9][0-9 ()\-.]*", phone):
        raise WhatsAppError("Informe o telefone com DDI e DDD, por exemplo +55 11 99999-9999.")
    digits = re.sub(r"[^0-9]", "", phone)
    if not 8 <= len(digits) <= 15 or digits.startswith("0"):
        raise WhatsAppError("Use de 8 a 15 dígitos, incluindo o DDI, sem zero antes do país.")
    if len(message) > 4000:
        raise WhatsAppError("Prepare uma mensagem de até 4.000 caracteres.")
    url = f"https://wa.me/{digits}"
    if message.strip():
        url += "?text=" + quote(message.strip(), safe="")
    return url


def _fold_with_positions(text: str) -> tuple[str, list[int]]:
    letters, positions = [], []
    for index, char in enumerate(text):
        for part in unicodedata.normalize("NFKD", char.lower()):
            if not unicodedata.combining(part):
                letters.append(part)
                positions.append(index)
    return "".join(letters), positions


class WhatsAppService:
    def __init__(self, opener=None):
        self._opener = opener if opener is not None else webbrowser.open

    def open_chat(self, phone: str = "", message: str = "") -> str:
        if not phone.strip() and message.strip():
            raise WhatsAppError("Informe o telefone do destinatário, com DDI e DDD.")
        url = chat_url(phone, message) if phone.strip() else "https://web.whatsapp.com/"
        try:
            opened = self._opener(url)
        except (OSError, webbrowser.Error):
            raise WhatsAppError("Não consegui abrir o navegador. Confira seu navegador padrão.") from None
        if not opened:
            raise WhatsAppError("O navegador não confirmou a abertura do WhatsApp. Tente novamente.")
        if message.strip():
            return "Abrindo a conversa com a mensagem preparada. Revise e toque em Enviar no WhatsApp."
        return "Abrindo a conversa no WhatsApp." if phone.strip() else "Abrindo o WhatsApp Web."

    def handle(self, text: str) -> str | None:
        raw = text.strip()
        folded, positions = _fold_with_positions(raw)
        prefix = r"(?:(?:apolo|por favor)[,\s]+)*"
        if re.fullmatch(prefix + r"ajuda (?:do |com o )?whats\s?app[.!?]*", folded):
            return ("Diga abra o WhatsApp ou prepare mensagem no WhatsApp para +55 DDD número "
                    "dizendo o texto. A mensagem fica pronta para você revisar e enviar.")
        if re.fullmatch(prefix + r"(?:(?:abra|abrir|abre|inicie) (?:o )?)?whats\s?app(?: web)?[.!?]*", folded):
            try:
                return self.open_chat()
            except WhatsAppError as exc:
                return str(exc)
        lead = (prefix + r"(?:(?:prepare|preparar|escreva|escrever|envie|mande) (?:uma )?)?"
                r"(?:mensagem )?(?:no |pelo |via )?whats\s?app(?: web)? (?:para|com|pra|pro) ")
        conversation = prefix + r"(?:abra|abrir|abre) (?:uma |a )?conversa no whats\s?app (?:com|para) "
        match = re.fullmatch(
            rf"(?:{lead}|{conversation})(?P<phone>\+?[0-9][0-9 ()\-.]*?)"
            r"(?:\s*(?::|\b(?:mensagem|dizendo|com a mensagem)\b)\s*(?P<message>.+))?", folded)
        if match:
            def original(group: str) -> str:
                start, end = match.span(group)
                return raw[positions[start]:positions[end - 1] + 1] if start >= 0 and end > start else ""
            try:
                return self.open_chat(original("phone"), original("message"))
            except WhatsAppError as exc:
                return str(exc)
        if re.match(rf"(?:{lead}|{conversation})", folded):
            return "Informe um telefone com DDI e DDD. Exemplo: WhatsApp para +55 11 99999-9999."
        return None
