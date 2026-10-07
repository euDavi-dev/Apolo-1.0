"""Comandos locais seguros.

Fluxo:  fala  ->  parse() (regras fixas)  ->  lista de comandos PERMITIDOS  ->  execute().

O Gemini nunca executa nada: só o que está nesta lista pode rodar, sem shell e com
argumentos fixos. Comandos perigosos (desligar/reiniciar) exigem confirmação falada.
Você pode permitir mais aplicativos em %APPDATA%\\APOLO\\commands.json (ver README)."""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from urllib.parse import quote_plus

from app.core.config import COMMANDS_FILE
from app.utils.textutils import normalize

log = logging.getLogger("apolo.commands")
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass(frozen=True)
class Command:
    id: str
    label: str
    aliases: tuple = ()
    argv: tuple | None = None        # executável + argumentos fixos (sem shell)
    start: str | None = None         # nome resolvido pelo Windows via "start" (ex.: chrome)
    url: str | None = None           # {q} é substituído pelo termo de busca (URL-encoded)
    dangerous: bool = False
    confirm: str = ""
    reply: str = ""


OPEN_VERBS = r"(?:abra|abrir|abre|inicie|iniciar|execute|executar|rode)"

BUILTIN = [
    Command("chrome", "o Chrome", ("google chrome", "chrome", "navegador"), start="chrome"),
    Command("youtube", "o YouTube", ("youtube", "you tube"), url="https://www.youtube.com"),
    Command("google", "o Google", ("google",), url="https://www.google.com"),
    Command("calculator", "a calculadora", ("calculadora",), argv=("calc.exe",)),
    Command("vscode", "o VS Code", ("visual studio code", "vs code", "vscode"), start="code"),
    Command("notepad", "o Bloco de Notas", ("bloco de notas", "notepad"), argv=("notepad.exe",)),
    Command("explorer", "o Explorador de Arquivos", ("explorador de arquivos", "explorador", "explorer"),
            argv=("explorer.exe",)),
]
LOCK = Command("lock", "", argv=("rundll32.exe", "user32.dll,LockWorkStation"), reply="Bloqueando o computador.")
SHUTDOWN = Command("shutdown", "", argv=("shutdown", "/s", "/t", "15", "/c", "APOLO: desligando em 15 segundos"),
                   dangerous=True, confirm="Tem certeza que deseja desligar o computador? Diga sim para confirmar.",
                   reply="Desligando em 15 segundos. Diga cancelar o desligamento para abortar.")
RESTART = Command("restart", "", argv=("shutdown", "/r", "/t", "15", "/c", "APOLO: reiniciando em 15 segundos"),
                  dangerous=True, confirm="Tem certeza que deseja reiniciar o computador? Diga sim para confirmar.",
                  reply="Reiniciando em 15 segundos. Diga cancelar o desligamento para abortar.")
CANCEL = Command("cancel_shutdown", "", argv=("shutdown", "/a"), reply="Desligamento cancelado.")
UNKNOWN = Command("unknown", "", reply="Esse aplicativo não está na minha lista de comandos permitidos. "
                                       "Você pode adicioná-lo no arquivo commands.json.")
SEARCH_SITES = {"google": "https://www.google.com/search?q={q}",
                "youtube": "https://www.youtube.com/results?search_query={q}"}

_MACHINE = r"(?:computador|pc|maquina|notebook)"
YES = re.compile(r"\b(sim|confirmo|confirmado|confirma|pode|claro|positivo|isso|com certeza)\b")
NO = re.compile(r"\b(nao|cancela|cancele|cancelar|negativo|deixa|esquece)\b")


def is_yes(text: str) -> bool:
    t = normalize(text)
    return bool(YES.search(t)) and not NO.search(t)


class CommandService:
    def __init__(self, ack=None):
        self.commands = list(BUILTIN) + self._load_custom()
        self.ack = ack            # função(label) -> frase de confirmação variada (personalidade)

    @staticmethod
    def _load_custom() -> list[Command]:
        """commands.json: [{"name": "Spotify", "aliases": ["spotify"], "path": "C:\\\\...\\\\Spotify.exe", "args": []}]"""
        try:
            items = json.loads(COMMANDS_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            log.exception("commands.json inválido")
            return []
        out = []
        for it in items:
            try:
                aliases = tuple(normalize(a) for a in it["aliases"])
                out.append(Command(f"custom:{it['name']}", f"o {it['name']}", aliases,
                                   argv=(it["path"], *map(str, it.get("args", [])))))
            except (KeyError, TypeError):
                log.warning("Entrada inválida em commands.json: %r", it)
        return out

    def parse(self, text: str) -> tuple[Command, str] | None:
        t = normalize(text)
        t = re.sub(r"^(?:apolo )?(?:por favor )?(?:voce )?(?:pode |poderia )?", "", t).strip()
        if re.search(rf"\b(?:cancele|cancelar|aborte|abortar)\b.*\b(?:desligamento|reinicializacao|desligar)\b", t):
            return CANCEL, ""
        if re.search(rf"\b(?:desligue|desligar|desliga)\b.*\b{_MACHINE}\b", t):
            return SHUTDOWN, ""
        if re.search(rf"\b(?:reinicie|reiniciar|reinicia)\b.*\b{_MACHINE}\b", t):
            return RESTART, ""
        if re.search(rf"\b(?:bloqueie|bloquear|bloqueia)\b.*\b(?:{_MACHINE}|tela)\b", t):
            return LOCK, ""
        m = re.match(r"(?:pesquise|pesquisar|procure|busque|buscar) (?:por )?(.+) (?:no|na|em) (google|youtube)$", t)
        if m:
            term, site = m.groups()
            return Command(f"search_{site}", f"a busca no {site}", url=SEARCH_SITES[site]), term
        m = re.match(rf"{OPEN_VERBS} (.+)$", t)
        if m:
            target = re.sub(r"^(?:o |a |os |as |um |uma )?(?:aplicativo |programa |app )?(?:o |a )?", "", m.group(1))
            for cmd in self.commands:
                for alias in cmd.aliases:
                    if re.search(rf"\b{re.escape(alias)}\b", target):
                        return cmd, ""
            return UNKNOWN, ""
        return None

    def execute(self, cmd: Command, arg: str = "") -> str:
        """Só recebe comandos vindos de parse() (lista permitida)."""
        try:
            if cmd.url:
                webbrowser.open(cmd.url.format(q=quote_plus(arg)) if "{q}" in cmd.url else cmd.url)
                if cmd.id.startswith("search_"):
                    return f"Pesquisando {arg}."
            elif cmd.start:
                subprocess.Popen(["cmd", "/c", "start", "", cmd.start], creationflags=NO_WINDOW)
            elif cmd.argv:
                subprocess.Popen(list(cmd.argv), creationflags=NO_WINDOW)
            else:
                return cmd.reply
        except (OSError, ValueError) as e:
            log.warning("Falha ao executar %s: %s", cmd.id, e)
            return f"Não consegui executar {cmd.label or 'o comando'}."
        return cmd.reply or (self.ack(cmd.label) if self.ack else f"Abrindo {cmd.label}.")
