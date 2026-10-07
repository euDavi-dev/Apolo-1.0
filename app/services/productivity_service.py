"""Tarefas, temporizadores e lembretes locais, sem rede ou execução de voz.

Cada operação abre sua própria conexão. A coleta de avisos usa uma transação
exclusiva de escrita para que dois consumidores nunca recebam o mesmo aviso.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Callable, Iterator
import unicodedata


class ProductivityError(ValueError):
    """Entrada inválida ou armazenamento de produtividade indisponível."""


MAX_TIMER_SECONDS = 30 * 24 * 60 * 60
MAX_TITLE_LENGTH = 300

_NUMBER_WORDS = {
    "zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2,
    "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7,
    "oito": 8, "nove": 9, "dez": 10, "onze": 11, "doze": 12,
    "treze": 13, "quatorze": 14, "catorze": 14, "quinze": 15,
    "dezesseis": 16, "dezasseis": 16, "dezessete": 17,
    "dezoito": 18, "dezenove": 19, "vinte": 20, "trinta": 30,
    "quarenta": 40, "cinquenta": 50, "sessenta": 60, "setenta": 70,
    "oitenta": 80, "noventa": 90, "cem": 100, "cento": 100,
    "duzentos": 200, "duzentas": 200, "trezentos": 300,
    "trezentas": 300, "quatrocentos": 400, "quatrocentas": 400,
    "quinhentos": 500, "quinhentas": 500, "seiscentos": 600,
    "seiscentas": 600, "setecentos": 700, "setecentas": 700,
    "oitocentos": 800, "oitocentas": 800, "novecentos": 900,
    "novecentas": 900,
}
_UNIT_SECONDS = {"h": 3600, "hora": 3600, "horas": 3600,
                 "min": 60, "mins": 60, "minuto": 60, "minutos": 60,
                 "s": 1, "seg": 1, "segs": 1, "segundo": 1, "segundos": 1}
_DURATION_PART = re.compile(
    r"(?P<number>.+?)(?:\s+|(?<=\d))"
    r"(?P<unit>horas?|minutos?|segundos?|mins?|segs?|seg|h|s)\b"
)
_HELP = (
    "Você pode dizer: adicionar tarefa Comprar pão; minhas tarefas; "
    "concluir tarefa 1; temporizador de 5 minutos; "
    "me lembre de beber água em 10 minutos; "
    "me lembre de uma reunião amanhã às 9h; meus lembretes; "
    "meus temporizadores; cancelar lembrete 2. "
    "Os avisos aparecem enquanto o Apolo está aberto."
)


def _normalized(text: str) -> tuple[str, list[int]]:
    """Normaliza somente para reconhecer comandos, conservando seus índices."""
    chars: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(text):
        folded = "".join(c for c in unicodedata.normalize("NFKD", char.casefold())
                         if not unicodedata.combining(c))
        for part in folded:
            chars.append(part)
            positions.append(index)
    return "".join(chars), positions


def _original_group(text: str, positions: list[int], match: re.Match,
                    group: int | str) -> str:
    start, end = match.span(group)
    if start < 0 or start == end:
        return ""
    stop = positions[end] if end < len(positions) else len(text)
    return text[positions[start]:stop].strip()


def _number(value: str) -> float:
    value = value.strip()
    if re.fullmatch(r"\d+(?:[.,]\d+)?", value):
        return float(value.replace(",", "."))
    if value in ("meio", "meia"):
        return 0.5
    words = value.split()
    if not words or words[0] == "e" or words[-1] == "e":
        raise ProductivityError("Não entendi a quantidade de tempo.")
    words = [word for word in words if word != "e"]
    if any(word not in _NUMBER_WORDS for word in words):
        raise ProductivityError("Não entendi a quantidade de tempo.")
    amounts = [_NUMBER_WORDS[word] for word in words]
    # Aceita 'cento e vinte e cinco', sem transformar 'cinco dez' em quinze.
    if len(amounts) > 3 or any(a <= b for a, b in zip(amounts, amounts[1:])):
        raise ProductivityError("Não entendi a quantidade de tempo.")
    if len(amounts) > 1 and (amounts[0] < 20 or amounts[-1] == 0):
        raise ProductivityError("Não entendi a quantidade de tempo.")
    return float(sum(amounts))


def _duration(value: str) -> float:
    value = value.strip().rstrip(".!?").strip()
    total = 0.0
    end = 0
    last_unit = 0
    for match in _DURATION_PART.finditer(value):
        if match.start() != end:
            raise ProductivityError("Não entendi a duração.")
        amount = match.group("number").strip()
        if end:
            amount = re.sub(r"^(?:,\s*|e\s+)", "", amount).strip()
        last_unit = _UNIT_SECONDS[match.group("unit")]
        total += _number(amount) * last_unit
        end = match.end()
    tail = value[end:].strip()
    if tail in ("e meia", "e meio") and last_unit:
        total += last_unit / 2
    elif tail or not end:
        raise ProductivityError("Não entendi a duração.")
    return total


class ProductivityService:
    def __init__(self, path: Path | None = None,
                 now: Callable[[], float] = time.time):
        if path is None:
            # A conta ativa pode alterar DATA_DIR depois de importar este módulo.
            from app.core import config
            path = config.DATA_DIR / "productivity.sqlite3"
        self.path = Path(path)
        self._now = now
        self._lock = threading.RLock()

    @contextmanager
    def _database(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = None
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL CHECK (kind IN ('task', 'timer', 'reminder')),
                        title TEXT NOT NULL,
                        due_at REAL,
                        created_at REAL NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'done', 'fired', 'cancelled'))
                    )
                """)
                connection.execute("CREATE INDEX IF NOT EXISTS items_due ON items(status, due_at)")
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection
                connection.commit()
            except (sqlite3.Error, OSError) as exc:
                raise ProductivityError(
                    "Não foi possível acessar ou salvar sua produtividade. "
                    "Verifique o espaço e a permissão da pasta de dados e tente novamente."
                ) from exc
            finally:
                if connection is not None:
                    # Fechar uma conexão com transação pendente desfaz a operação.
                    connection.close()

    @staticmethod
    def _title(title: str) -> str:
        if not isinstance(title, str) or not title.strip():
            raise ProductivityError("Informe um título para o item.")
        title = title.strip()
        if len(title) > MAX_TITLE_LENGTH:
            raise ProductivityError(f"Use um título de até {MAX_TITLE_LENGTH} caracteres.")
        if any(unicodedata.category(char) == "Cc" for char in title):
            raise ProductivityError("Use um título sem caracteres de controle.")
        return title

    @staticmethod
    def _finite(value: float, description: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProductivityError(f"{description} precisa ser um número válido.")
        try:
            converted = float(value)
        except (ValueError, OverflowError) as exc:
            raise ProductivityError(f"{description} precisa ser um número finito.") from exc
        if not math.isfinite(converted):
            raise ProductivityError(f"{description} precisa ser um número finito.")
        return converted

    def _current_time(self) -> float:
        return self._finite(self._now(), "O horário atual")

    def _insert(self, kind: str, title: str, due_at: float | None,
                created_at: float) -> dict:
        title = self._title(title)
        with self._database(write=True) as db:
            cursor = db.execute(
                "INSERT INTO items (kind, title, due_at, created_at) VALUES (?, ?, ?, ?)",
                (kind, title, due_at, created_at),
            )
            item = dict(db.execute("SELECT * FROM items WHERE id = ?",
                                   (cursor.lastrowid,)).fetchone())
        return item

    def add_task(self, title: str) -> dict:
        return self._insert("task", title, None, self._current_time())

    def add_timer(self, seconds: float, title: str = "Temporizador") -> dict:
        seconds = self._finite(seconds, "A duração")
        if not 0 < seconds <= MAX_TIMER_SECONDS:
            raise ProductivityError("A duração deve ser maior que zero e de até 30 dias.")
        created_at = self._current_time()
        due_at = self._finite(created_at + seconds, "O horário do temporizador")
        return self._insert("timer", title, due_at, created_at)

    def add_reminder(self, title: str, due_at: float) -> dict:
        due_at = self._finite(due_at, "O horário do lembrete")
        try:
            datetime.fromtimestamp(due_at)
        except (OverflowError, OSError, ValueError) as exc:
            raise ProductivityError("O horário do lembrete precisa ser uma data válida.") from exc
        created_at = self._current_time()
        if due_at <= created_at:
            raise ProductivityError("Esse horário já passou. Escolha um horário futuro para o lembrete.")
        return self._insert("reminder", title, due_at, created_at)

    @staticmethod
    def _identifier(item_id: int) -> int:
        if (isinstance(item_id, bool) or not isinstance(item_id, int)
                or not 0 < item_id <= 9223372036854775807):
            raise ProductivityError("Informe o número válido do item, como 1.")
        return item_id

    @classmethod
    def _spoken_identifier(cls, number: str, usage: str) -> int:
        if not re.fullmatch(r"\d+", number) or len(number) > 19:
            raise ProductivityError(usage)
        return cls._identifier(int(number))

    def _change_status(self, item_id: int, status: str,
                       expected_kind: str | None = None) -> dict:
        item_id = self._identifier(item_id)
        with self._database(write=True) as db:
            row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise ProductivityError(f"Não encontrei o item {item_id}.")
            item = dict(row)
            if expected_kind is not None and item["kind"] != expected_kind:
                names = {"task": "tarefa", "timer": "temporizador", "reminder": "lembrete"}
                article = "uma" if item["kind"] == "task" else "um"
                raise ProductivityError(
                    f"O item {item_id} é {article} {names[item['kind']]}. "
                    f"Use o comando com o tipo correto."
                )
            if item["status"] == status:
                return item
            if item["status"] != "active":
                raise ProductivityError(f"O item {item_id} já foi encerrado.")
            db.execute("UPDATE items SET status = ? WHERE id = ?", (status, item_id))
            item["status"] = status
        return item

    def complete_task(self, item_id: int) -> dict:
        return self._change_status(item_id, "done", "task")

    def cancel(self, item_id: int) -> dict:
        return self._change_status(item_id, "cancelled")

    def list_items(self, include_completed: bool = False) -> list[dict]:
        clause = "status != 'cancelled'" if include_completed else "status = 'active'"
        with self._database() as db:
            return [dict(row) for row in db.execute(
                f"SELECT * FROM items WHERE {clause} ORDER BY created_at, id"
            ).fetchall()]

    def poll_due(self) -> list[dict]:
        now = self._current_time()
        with self._database(write=True) as db:
            rows = db.execute(
                "SELECT * FROM items WHERE status = 'active' "
                "AND kind IN ('timer', 'reminder') AND due_at <= ? ORDER BY due_at, id",
                (now,),
            ).fetchall()
            db.execute(
                "UPDATE items SET status = 'fired' WHERE status = 'active' "
                "AND kind IN ('timer', 'reminder') AND due_at <= ?", (now,),
            )
            items = [dict(row, status="fired") for row in rows]
        return items

    def requeue_due(self, item_ids: list[int]) -> None:
        """Devolve avisos coletados pelo consumidor e ainda não publicados.

        O consumidor deve fornecer somente os IDs de sua própria coleta. Itens
        cancelados, tarefas e registros já concluídos nunca são reabertos.
        """
        if not isinstance(item_ids, list):
            raise ProductivityError("Informe uma lista de números válidos dos avisos.")
        identifiers = [self._identifier(item_id) for item_id in item_ids]
        if not identifiers:
            return
        with self._database(write=True) as db:
            db.executemany(
                "UPDATE items SET status = 'active' WHERE id = ? AND status = 'fired' "
                "AND kind IN ('timer', 'reminder')",
                ((item_id,) for item_id in identifiers),
            )

    def close(self) -> None:
        """Idempotente: as conexões são fechadas ao concluir cada operação."""

    @staticmethod
    def _due_label(due_at: float) -> str:
        try:
            return datetime.fromtimestamp(due_at).strftime("%d/%m às %H:%M")
        except (OverflowError, OSError, ValueError):
            return "no horário agendado"

    def _list_kind(self, kind: str) -> str:
        names = {"task": "tarefas", "timer": "temporizadores", "reminder": "lembretes"}
        items = [item for item in self.list_items() if item["kind"] == kind]
        if not items:
            adjective = "ativas" if kind == "task" else "ativos"
            return f"Você não tem {names[kind]} {adjective}."
        descriptions = []
        for item in items:
            entry = f"{item['id']}: {item['title']}"
            if item["due_at"] is not None:
                entry += f", {self._due_label(item['due_at'])}"
            descriptions.append(entry)
        pronoun = "Suas" if kind == "task" else "Seus"
        return f"{pronoun} {names[kind]}: " + "; ".join(descriptions) + "."

    def _calendar_due(self, day: str, clock: str) -> float:
        clock = clock.strip().rstrip(".!?").strip()
        match = re.fullmatch(r"(\d{1,2})(?:h(\d{1,2})?|:(\d{1,2}))?(?:\s*horas?)?", clock)
        if match is None:
            raise ProductivityError("Use um horário como 9h ou 15:30.")
        hour, minute = int(match.group(1)), int(match.group(2) or match.group(3) or 0)
        if hour > 23 or minute > 59:
            raise ProductivityError("Use um horário válido entre 00:00 e 23:59.")
        try:
            target = datetime.fromtimestamp(self._current_time()).replace(
                hour=hour, minute=minute, second=0, microsecond=0,
            )
            if day == "amanha":
                target += timedelta(days=1)
            return target.timestamp()
        except (OverflowError, OSError, ValueError) as exc:
            raise ProductivityError("Não foi possível interpretar esse horário.") from exc

    def handle(self, text: str) -> str | None:
        """Responde a comandos explícitos; perguntas gerais seguem para a IA."""
        if not isinstance(text, str) or not text.strip():
            return None
        original = text.strip()
        normalized, positions = _normalized(original)
        prefix = re.match(r"^(?:apolo[,\s]+)?(?:por favor[,\s]+)?", normalized)
        if prefix is not None and prefix.end():
            if prefix.end() >= len(positions):
                return None
            original = original[positions[prefix.end()]:].strip()
            normalized, positions = _normalized(original)
        exact = normalized.rstrip(".!?").strip()
        try:
            if re.fullmatch(r"ajuda (?:com |de |sobre )?produtividade", exact):
                return _HELP
            for pattern, kind in (
                (r"(?:minhas tarefas|listar tarefas|liste (?:minhas |as )?tarefas)", "task"),
                (r"(?:meus lembretes|listar lembretes|liste (?:meus |os )?lembretes)", "reminder"),
                (r"(?:meus temporizadores|listar temporizadores|liste (?:meus |os )?temporizadores)", "timer"),
            ):
                if re.fullmatch(pattern, exact):
                    return self._list_kind(kind)
            match = re.fullmatch(
                r"(?:adicionar|adicione|criar|crie)\s+(?:uma\s+)?tarefa(?:\s+(.*))?",
                normalized,
            )
            if match:
                title = _original_group(original, positions, match, 1)
                item = self.add_task(title)
                return f"Tarefa {item['id']} adicionada: {item['title']}."
            match = re.fullmatch(
                r"(?:concluir|conclua)\s+(?:a\s+)?tarefa(?:\s+(.*))?", exact,
            )
            if match:
                number = match.group(1) or ""
                item_id = self._spoken_identifier(
                    number, "Diga concluir tarefa seguido do número, como concluir tarefa 1."
                )
                item = self.complete_task(item_id)
                return f"Tarefa {item['id']} concluída: {item['title']}."
            match = re.fullmatch(
                r"(?:cancelar|cancele)\s+(?:o\s+|a\s+)?(tarefa|temporizador|lembrete)(?:\s+(.*))?",
                exact,
            )
            if match:
                number = match.group(2) or ""
                item_id = self._spoken_identifier(
                    number, "Diga cancelar, o tipo e o número, como cancelar lembrete 2."
                )
                kind = {"tarefa": "task", "temporizador": "timer", "lembrete": "reminder"}[match.group(1)]
                item = self._change_status(item_id, "cancelled", kind)
                return f"Item {item['id']} cancelado: {item['title']}."
            match = re.fullmatch(
                r"(?:(?:iniciar|inicie|criar|crie|definir|defina)\s+)?(?:um\s+)?"
                r"temporizador(?:\s+(?:de|por))?(?:\s+(.*))?", normalized,
            )
            if match:
                try:
                    seconds = _duration(match.group(1) or "")
                except ProductivityError as exc:
                    raise ProductivityError(
                        "Diga temporizador de 5 minutos, ou temporizador de 1 hora e 30 minutos."
                    ) from exc
                item = self.add_timer(seconds)
                return f"Temporizador {item['id']} iniciado, até {self._due_label(item['due_at'])}."
            if re.match(r"^(?:me lembre|lembre-me)\s+de(?:\s|$)", normalized):
                prefix = r"(?:me lembre|lembre-me)\s+de\s+"
                separators = list(re.finditer(r"\s+(em|as)\s+", normalized))
                is_relative = bool(separators and separators[-1].group(1) == "em")
                # O último separador indica a agenda: 'ir às compras em dez
                # minutos' e 'treinar em casa amanhã às 9h' conservam o título.
                match = (None if is_relative else re.fullmatch(
                    prefix + r"(.+)\s+(hoje|amanha)\s+as\s+(.+)", normalized,
                ))
                if match:
                    title = _original_group(original, positions, match, 1)
                    due_at = self._calendar_due(match.group(2), match.group(3))
                else:
                    match = (None if is_relative else re.fullmatch(
                        prefix + r"(.+)\s+as\s+(.+)", normalized,
                    ))
                    if match:
                        title = _original_group(original, positions, match, 1)
                        due_at = self._calendar_due("hoje", match.group(2))
                    else:
                        match = re.fullmatch(prefix + r"(.+)\s+em\s+(.+)", normalized)
                        if not match:
                            raise ProductivityError(
                                "Diga me lembre de beber água em 10 minutos, "
                                "ou me lembre de uma reunião amanhã às 9h."
                            )
                        title = _original_group(original, positions, match, 1)
                        try:
                            seconds = _duration(match.group(2))
                        except ProductivityError as exc:
                            raise ProductivityError("Diga me lembre de beber água em 10 minutos.") from exc
                        if not 0 < seconds <= MAX_TIMER_SECONDS:
                            raise ProductivityError("O intervalo deve ser maior que zero e de até 30 dias.")
                        due_at = self._current_time() + seconds
                item = self.add_reminder(title, due_at)
                return f"Lembrete {item['id']} agendado: {item['title']}, {self._due_label(item['due_at'])}."
        except ProductivityError as exc:
            return str(exc)
        return None
