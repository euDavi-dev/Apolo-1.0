"""Produtividade local: persistência, concorrência e comandos sem serviços externos."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import math
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.services.productivity_service import (
    MAX_TIMER_SECONDS, MAX_TITLE_LENGTH, ProductivityError, ProductivityService,
)


class ProductivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "profile" / "productivity.sqlite3"
        self.now = 1000.0
        self.service = ProductivityService(self.path, now=lambda: self.now)
        self.addCleanup(self.service.close)

    def test_constructor_is_lazy_and_close_is_idempotent(self):
        self.assertFalse(self.path.parent.exists())
        self.service.close()
        self.service.close()
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.service.add_task("Comprar pão")["status"], "active")

    def test_default_path_follows_current_account_data_dir(self):
        from app.core import config
        first_dir = Path(self.temp.name) / "first"
        second_dir = Path(self.temp.name) / "second"
        with patch.object(config, "DATA_DIR", first_dir):
            first = ProductivityService(now=lambda: self.now)
        with patch.object(config, "DATA_DIR", second_dir):
            second = ProductivityService(now=lambda: self.now)
        self.assertEqual(first.path, first_dir / "productivity.sqlite3")
        self.assertEqual(second.path, second_dir / "productivity.sqlite3")
        self.assertFalse(first_dir.exists())
        self.assertFalse(second_dir.exists())
        first.add_task("Só na primeira conta")
        self.assertEqual(second.list_items(), [])

    def test_rows_have_documented_fields_and_values(self):
        task = self.service.add_task("  Revisar currículo!  ")
        timer = self.service.add_timer(90, "Café")
        reminder = self.service.add_reminder("Beber água", 1050)
        expected = {"id", "kind", "title", "due_at", "created_at", "status"}
        for row in (task, timer, reminder):
            self.assertEqual(set(row), expected)
            self.assertIsInstance(row["id"], int)
            self.assertEqual(row["created_at"], 1000)
            self.assertEqual(row["status"], "active")
        self.assertEqual(task["kind"], "task")
        self.assertEqual(task["title"], "Revisar currículo!")
        self.assertIsNone(task["due_at"])
        self.assertEqual(timer["due_at"], 1090)
        self.assertEqual(reminder["kind"], "reminder")

    def test_persistence_isolation_and_status_filters(self):
        task = self.service.add_task("Pendência")
        done = self.service.add_task("Concluída")
        cancelled = self.service.add_timer(5)
        fired = self.service.add_reminder("Água", 1003)
        self.service.complete_task(done["id"])
        self.service.cancel(cancelled["id"])
        self.now = 1004
        self.assertEqual(self.service.poll_due()[0]["id"], fired["id"])
        restarted = ProductivityService(self.path, now=lambda: self.now)
        self.assertEqual([x["id"] for x in restarted.list_items()], [task["id"]])
        history = restarted.list_items(include_completed=True)
        self.assertEqual([x["status"] for x in history], ["active", "done", "fired"])
        self.assertEqual(restarted.poll_due(), [])
        other = ProductivityService(Path(self.temp.name) / "other.sqlite3")
        self.assertEqual(other.list_items(True), [])

    def test_poll_due_delivers_once_in_due_order_and_leaves_tasks(self):
        task = self.service.add_task("Tarefa")
        later = self.service.add_timer(20)
        sooner = self.service.add_reminder("Reunião", 1010)
        self.now = 1010
        due = self.service.poll_due()
        self.assertEqual([x["id"] for x in due], [sooner["id"]])
        self.assertEqual(due[0]["status"], "fired")
        self.assertEqual(self.service.poll_due(), [])
        self.now = 1030
        self.assertEqual(self.service.poll_due()[0]["id"], later["id"])
        self.assertEqual(self.service.list_items()[0]["id"], task["id"])

    def test_poll_due_is_atomic_across_instances_and_threads(self):
        rows = [self.service.add_timer(5, f"Aviso {index}") for index in range(40)]
        self.now = 1006
        workers = 8
        barrier = threading.Barrier(workers)

        def poll(index):
            service = ProductivityService(self.path, now=lambda: self.now)
            barrier.wait(timeout=10)
            return service.poll_due()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            batches = list(pool.map(poll, range(workers)))
        delivered = [item["id"] for batch in batches for item in batch]
        self.assertEqual(len(delivered), len(rows))
        self.assertEqual(set(delivered), {item["id"] for item in rows})
        self.assertTrue(all(item["status"] == "fired" for batch in batches for item in batch))
        self.assertEqual(self.service.poll_due(), [])

    def test_requeue_undelivered_due_items_are_delivered_once_after_restart(self):
        timer = self.service.add_timer(5)
        reminder = self.service.add_reminder("Aviso", 1005)
        self.now = 1010
        polled = self.service.poll_due()
        self.assertEqual({item["id"] for item in polled}, {timer["id"], reminder["id"]})
        self.service.requeue_due([item["id"] for item in polled])
        restarted = ProductivityService(self.path, now=lambda: self.now)
        delivered = restarted.poll_due()
        self.assertEqual({item["id"] for item in delivered}, {timer["id"], reminder["id"]})
        self.assertEqual(restarted.poll_due(), [])

    def test_requeue_empty_batch_does_not_open_database(self):
        self.service.requeue_due([])
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_requeue_never_reopens_tasks_done_cancelled_or_changes_active_items(self):
        task = self.service.add_task("Ativa")
        done = self.service.complete_task(self.service.add_task("Concluída")["id"])
        cancelled = self.service.cancel(self.service.add_reminder("Cancelado", 1005)["id"])
        active = self.service.add_timer(50)
        # Mesmo um banco antigo com tarefa marcada fired não pode reabri-la.
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE items SET status = 'fired' WHERE id = ?", (task["id"],))
            db.commit()
        ids = [item["id"] for item in (task, done, cancelled, active)]
        self.service.requeue_due(ids + [999])
        history = {item["id"]: item for item in self.service.list_items(True)}
        self.assertEqual(history[task["id"]]["status"], "fired")
        self.assertEqual(history[done["id"]]["status"], "done")
        self.assertEqual(history[active["id"]]["status"], "active")
        self.assertNotIn(cancelled["id"], history)
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT status FROM items WHERE id = ?", (cancelled["id"],)).fetchone()
        self.assertEqual(row[0], "cancelled")

    def test_requeue_validates_whole_batch_before_any_write(self):
        timer = self.service.add_timer(5)
        self.now = 1010
        self.service.poll_due()
        for invalid in (None, (timer["id"],), [timer["id"], 0], [timer["id"], True],
                        [timer["id"], "2"], [timer["id"], 2 ** 63]):
            with self.subTest(invalid=invalid), self.assertRaises(ProductivityError):
                self.service.requeue_due(invalid)
        self.assertEqual(self.service.list_items(True)[0]["status"], "fired")

    def test_failed_requeue_batch_rolls_back_every_item(self):
        first = self.service.add_timer(5)
        second = self.service.add_reminder("Aviso", 1005)
        self.now = 1010
        self.service.poll_due()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                f"CREATE TRIGGER reject_second BEFORE UPDATE ON items WHEN OLD.id = {second['id']} "
                "BEGIN SELECT RAISE(ABORT, 'failed write'); END"
            )
        with self.assertRaises(ProductivityError):
            self.service.requeue_due([first["id"], second["id"]])
        self.assertEqual([item["status"] for item in self.service.list_items(True)], ["fired", "fired"])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TRIGGER reject_second")
        self.service.requeue_due([first["id"], second["id"]])
        self.assertEqual(len(self.service.poll_due()), 2)
        self.assertEqual(self.service.poll_due(), [])

    def test_additions_are_thread_safe(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda i: self.service.add_task(f"Tarefa {i}"), range(40)))
        self.assertEqual(len({row["id"] for row in rows}), 40)
        self.assertEqual(len(self.service.list_items()), 40)

    def test_invalid_titles_do_not_create_items(self):
        for title in (None, 123, "", "   ", "x" * (MAX_TITLE_LENGTH + 1), "Título\x00oculto"):
            with self.subTest(title=repr(title)[:40]), self.assertRaises(ProductivityError):
                self.service.add_task(title)
        self.assertFalse(self.path.exists())
        title = "Á" * MAX_TITLE_LENGTH
        self.assertEqual(self.service.add_task(title)["title"], title)

    def test_timer_limits_and_nonfinite_values(self):
        for seconds in (0, -1, math.nan, math.inf, -math.inf, True, "5", None,
                        MAX_TIMER_SECONDS + 0.1, 10 ** 1000):
            with self.subTest(seconds=str(seconds)[:40]), self.assertRaises(ProductivityError):
                self.service.add_timer(seconds)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.service.add_timer(MAX_TIMER_SECONDS)["due_at"],
                         self.now + MAX_TIMER_SECONDS)
        self.assertEqual(self.service.add_timer(0.1)["due_at"], self.now + 0.1)

    def test_reminders_must_have_future_finite_times(self):
        for due_at in (999, 1000, math.nan, math.inf, -math.inf, True, "1001", None, 1e100):
            with self.subTest(due_at=due_at), self.assertRaises(ProductivityError):
                self.service.add_reminder("Aviso", due_at)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.service.add_reminder("Aviso", 1000.01)["due_at"], 1000.01)

    def test_invalid_ids_and_missing_items(self):
        for item_id in (0, -1, True, "1", 1.0, None, 2 ** 63):
            for action in (self.service.cancel, self.service.complete_task):
                with self.subTest(item_id=item_id), self.assertRaises(ProductivityError):
                    action(item_id)
        with self.assertRaisesRegex(ProductivityError, "Não encontrei"):
            self.service.cancel(1)

    def test_complete_and_cancel_validate_kind_and_state(self):
        task = self.service.add_task("Tarefa")
        timer = self.service.add_timer(10)
        with self.assertRaisesRegex(ProductivityError, "tipo correto"):
            self.service.complete_task(timer["id"])
        self.assertEqual(self.service.complete_task(task["id"])["status"], "done")
        self.assertEqual(self.service.complete_task(task["id"])["status"], "done")
        with self.assertRaisesRegex(ProductivityError, "encerrado"):
            self.service.cancel(task["id"])
        self.assertEqual(self.service.cancel(timer["id"])["status"], "cancelled")
        self.assertEqual(self.service.cancel(timer["id"])["status"], "cancelled")

    def test_storage_permission_failure_is_reported_without_success(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError("bloqueado")):
            with self.assertRaisesRegex(ProductivityError, "Não foi possível"):
                self.service.add_task("Não foi salva")
            result = self.service.handle("adicionar tarefa Também não foi salva")
        self.assertIn("Não foi possível", result)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.service.list_items(), [])

    def test_corrupt_database_is_reported_and_preserved(self):
        self.path.parent.mkdir()
        contents = b"Banco corrompido que deve permanecer intacto"
        self.path.write_bytes(contents)
        with self.assertRaises(ProductivityError):
            self.service.add_task("Não salva")
        self.assertEqual(self.path.read_bytes(), contents)
        self.assertIn("Não foi possível", self.service.handle("minhas tarefas"))

    def test_failed_insert_does_not_lose_existing_records(self):
        existing = self.service.add_task("Preservar")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER reject_new BEFORE INSERT ON items "
                       "BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        with self.assertRaises(ProductivityError):
            self.service.add_task("Não salva")
        self.assertEqual(self.service.list_items(), [existing])

    def test_failed_due_update_is_rolled_back_and_can_be_retried(self):
        item = self.service.add_timer(5)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON items "
                       "BEGIN SELECT RAISE(ABORT, 'failed write'); END")
        self.now = 1010
        with self.assertRaises(ProductivityError):
            self.service.poll_due()
        self.assertEqual(self.service.list_items()[0]["status"], "active")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TRIGGER reject_update")
        self.assertEqual(self.service.poll_due()[0]["id"], item["id"])
        self.assertEqual(self.service.poll_due(), [])

    def test_failed_commit_is_rolled_back(self):
        existing = self.service.add_task("Preservar")
        real_connect = sqlite3.connect

        class BrokenCommit:
            def __init__(self, *args, **kwargs):
                object.__setattr__(self, "connection", real_connect(*args, **kwargs))

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def __setattr__(self, name, value):
                setattr(self.connection, name, value)

            def commit(self):
                raise sqlite3.OperationalError("disk full at commit")

        with patch("app.services.productivity_service.sqlite3.connect", BrokenCommit):
            with self.assertRaises(ProductivityError):
                self.service.add_task("Não confirmada")
        self.assertEqual(self.service.list_items(), [existing])

    def test_command_titles_preserve_accents_case_and_punctuation(self):
        result = self.service.handle("ADICIONAR TAREFA Comprar Café, pão e maçãs!")
        self.assertIn("Tarefa 1 adicionada", result)
        self.assertEqual(self.service.list_items()[0]["title"], "Comprar Café, pão e maçãs!")
        self.service.handle("me lembre de Ver Clima! em dez minutos.")
        self.assertEqual(self.service.list_items()[1]["title"], "Ver Clima!")
        decomposed = "Cafe\u0301"
        self.service.handle(f"adicionar tarefa {decomposed}")
        self.assertEqual(self.service.list_items()[2]["title"], decomposed)

    def test_timer_duration_numbers_words_and_compounds(self):
        examples = {
            "5 minutos": 300, "cinco minutos": 300, "três segundos": 3,
            "vinte e cinco minutos": 1500, "1 hora e 30 minutos": 5400,
            "duas horas e dez minutos e cinco segundos": 7805,
            "1,5 minuto": 90, "meia hora": 1800, "uma hora e meia": 5400,
            "5min": 300, "um minuto e meio": 90, "1 hora, 20 minutos": 4800,
        }
        for duration, seconds in examples.items():
            with self.subTest(duration=duration):
                result = self.service.handle(f"temporizador de {duration}")
                self.assertIn("iniciado", result)
                self.assertEqual(self.service.list_items()[-1]["due_at"], self.now + seconds)

    def test_invalid_known_commands_return_local_usage_or_error(self):
        examples = (
            "temporizador", "temporizador de batata", "temporizador de -5 minutos",
            "temporizador de 0 segundos", "temporizador de 721 horas",
            "temporizador de 5", "temporizador de cinco dez minutos",
            "me lembre de beber água", "me lembre de beber água em 0 segundos",
            "me lembre de beber água em muitos minutos", "adicionar tarefa",
            "concluir tarefa", "cancelar tarefa abc", "cancelar lembrete -1",
            "cancelar tarefa " + "9" * 5000, "concluir tarefa " + "9" * 5000,
        )
        for command in examples:
            with self.subTest(command=command):
                self.assertIsInstance(self.service.handle(command), str)
        self.assertEqual(self.service.list_items(), [])

    def test_general_questions_are_not_captured(self):
        for text in ("explique temporizadores", "como organizar tarefas",
                     "Quais as vantagens de lembretes?", "Qual é o clima?", "", None):
            with self.subTest(text=text):
                self.assertIsNone(self.service.handle(text))
        self.assertFalse(self.path.exists())

    def test_task_completion_cancellation_and_listing_commands(self):
        self.assertIn("não tem", self.service.handle("minhas tarefas"))
        self.service.handle("adicionar tarefa Comprar pão")
        self.assertIn("1: Comprar pão", self.service.handle("listar tarefas."))
        self.assertIn("concluída", self.service.handle("concluir tarefa 1."))
        self.assertIn("não tem", self.service.handle("minhas tarefas"))
        self.service.handle("temporizador de 5 minutos")
        self.assertIn("2: Temporizador", self.service.handle("meus temporizadores"))
        self.service.handle("me lembre de Água em dez minutos")
        self.assertIn("3: Água", self.service.handle("meus lembretes"))
        self.assertIn("cancelado", self.service.handle("cancelar lembrete 3"))
        self.assertIn("não tem", self.service.handle("meus lembretes"))

    def test_cancel_command_rejects_wrong_kind_without_changing_item(self):
        task = self.service.add_task("Tarefa")
        timer = self.service.add_timer(5)
        reminder = self.service.add_reminder("Aviso", 1005)
        for noun, item in (("temporizador", task), ("lembrete", timer), ("tarefa", reminder)):
            with self.subTest(noun=noun):
                self.assertIn("tipo correto", self.service.handle(f"cancelar {noun} {item['id']}"))
        self.assertTrue(all(item["status"] == "active" for item in self.service.list_items()))

    def test_relative_reminder_keeps_title_with_em_inside(self):
        self.service.handle("me lembre de Encontrar João em casa em 10 minutos")
        item = self.service.list_items()[0]
        self.assertEqual(item["title"], "Encontrar João em casa")
        self.assertEqual(item["due_at"], 1600)

    def test_relative_reminder_keeps_as_inside_title(self):
        self.service.handle("me lembre de Ir às compras em dez minutos")
        item = self.service.list_items()[0]
        self.assertEqual(item["title"], "Ir às compras")
        self.assertEqual(item["due_at"], 1600)
        self.service.handle("me lembre de Confirmar reunião amanhã às 9h em cinco minutos")
        item = self.service.list_items()[1]
        self.assertEqual(item["title"], "Confirmar reunião amanhã às 9h")
        self.assertEqual(item["due_at"], 1300)

    def test_calendar_reminders_today_tomorrow_and_clock_only(self):
        self.now = datetime(2026, 10, 6, 8, 0).timestamp()
        examples = (
            ("me lembre de Reunião amanhã às 9h", datetime(2026, 10, 7, 9, 0)),
            ("me lembre de Médico hoje às 15:30", datetime(2026, 10, 6, 15, 30)),
            ("me lembre de Café às 9h30", datetime(2026, 10, 6, 9, 30)),
            ("me lembre de Água às 10 horas", datetime(2026, 10, 6, 10, 0)),
            ("me lembre de Treinar em casa amanhã às 9h", datetime(2026, 10, 7, 9, 0)),
        )
        for command, expected in examples:
            with self.subTest(command=command):
                self.assertIn("agendado", self.service.handle(command))
                self.assertEqual(self.service.list_items()[-1]["due_at"], expected.timestamp())

    def test_past_and_invalid_clock_commands_do_not_create_reminders(self):
        self.now = datetime(2026, 10, 6, 15, 30).timestamp()
        for clock in ("hoje às 14h", "às 15:30", "amanhã às 24h", "às 15:60", "às cedo", "amanhã às 9:"):
            with self.subTest(clock=clock):
                self.assertIsInstance(self.service.handle(f"me lembre de Médico {clock}"), str)
        self.assertEqual(self.service.list_items(), [])

    def test_tomorrow_handles_end_of_month(self):
        self.now = datetime(2026, 12, 31, 23, 0).timestamp()
        self.service.handle("me lembre de Feliz ano novo amanhã às 9h")
        self.assertEqual(self.service.list_items()[0]["due_at"], datetime(2027, 1, 1, 9).timestamp())

    def test_help_mentions_supported_commands_and_open_app(self):
        result = self.service.handle("ajuda produtividade")
        self.assertIn("concluir tarefa 1", result)
        self.assertIn("me lembre", result)
        self.assertIn("aberto", result)
        self.assertFalse(self.path.exists())

    def test_polite_and_wake_word_prefixes_keep_original_title(self):
        self.service.handle("Apolo, por favor, adicionar tarefa Café, Maçã e Pão!")
        self.service.handle("por favor me lembre de Água em dez minutos")
        self.assertEqual(self.service.list_items()[0]["title"], "Café, Maçã e Pão!")
        self.assertEqual(self.service.list_items()[1]["title"], "Água")
        self.assertIsNone(self.service.handle("Apolo, por favor, como organizar tarefas?"))


if __name__ == "__main__":
    unittest.main()
