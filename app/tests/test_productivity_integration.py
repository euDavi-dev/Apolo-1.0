"""Integração de produtividade sem microfone, rede, TTS ou dados reais.

Execute: python -m unittest app.tests.test_productivity_integration
"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app.core.assistant as assistant_module
from app.core.assistant import Assistant
from app.core.config import Settings
from app.core.state import State
from app.services.productivity_service import ProductivityError, ProductivityService


SERVICE_CONSTRUCTORS = (
    "GeminiService", "WeatherService", "CommandService", "SpeechService",
    "TTSService", "AudioEngine", "ClapConfig", "ClapDetector", "WhisperWakeSTT",
    "WakeWordDetector", "StreamingKeywordSpotter", "MusicService", "ActivationAudio",
    "WhatsAppService",
)


class ProductivityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1_800_000_000.0

    def service(self):
        service = ProductivityService(Path(self.directory.name) / "productivity.sqlite3",
                                      now=lambda: self.now)
        self.addCleanup(service.close)
        return service

    def assistant(self, service=None):
        service = self.service() if service is None else service
        with ExitStack() as patches:
            for constructor in SERVICE_CONSTRUCTORS:
                patches.enter_context(patch.object(assistant_module, constructor))
            default_service = patches.enter_context(patch.object(assistant_module, "ProductivityService"))
            assistant = Assistant(Settings(), productivity=service)
            default_service.assert_not_called()
        self.assertIs(assistant.productivity, service)
        assistant.music.parse.return_value = None
        assistant.commands.parse.return_value = None
        assistant.whatsapp.handle.return_value = None
        assistant._reply = Mock()
        assistant._fail = Mock()
        assistant._ask_gemini = Mock()
        assistant.tts.reset_mock()
        assistant.events, assistant.snapshots, assistant.alerts, assistant.states = [], [], [], []
        assistant.message_added.connect(lambda role, text: assistant.events.append((role, text)))
        assistant.productivity_changed.connect(assistant.snapshots.append)
        assistant.productivity_alert.connect(assistant.alerts.append)
        assistant.state_changed.connect(assistant.states.append)
        return assistant

    def assert_local(self, assistant, text):
        self.assertFalse(assistant._handle_text(text))
        assistant.music.parse.assert_not_called()
        assistant.commands.parse.assert_not_called()
        assistant.whatsapp.handle.assert_not_called()
        assistant._ask_gemini.assert_not_called()
        assistant._reply.assert_called_once()

    def test_task_is_saved_without_external_services(self):
        assistant = self.assistant()
        self.assert_local(assistant, "adicionar tarefa Comprar pão")
        items = assistant.productivity.list_items()
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["kind"], items[0]["title"]), ("task", "Comprar pão"))
        self.assertEqual(assistant.snapshots[-1], items)

    def test_reminder_with_weather_word_stays_local(self):
        assistant = self.assistant()
        self.assert_local(assistant, "me lembre de ver clima em 10 minutos")
        item = assistant.productivity.list_items()[0]
        self.assertEqual(item["kind"], "reminder")
        self.assertEqual(item["due_at"], self.now + 600)

    def test_timer_stays_local_and_has_real_deadline(self):
        assistant = self.assistant()
        self.assert_local(assistant, "temporizador de 5 minutos")
        item = assistant.productivity.list_items()[0]
        self.assertEqual(item["kind"], "timer")
        self.assertEqual(item["due_at"], self.now + 300)

    def test_invalid_duration_does_not_create_item_or_use_ai(self):
        assistant = self.assistant()
        self.assert_local(assistant, "temporizador de 0 minutos")
        self.assertEqual(assistant.productivity.list_items(True), [])
        self.assertNotIn("iniciado", assistant._reply.call_args.args[0].lower())

    def test_normal_question_continues_to_ai(self):
        assistant = self.assistant()
        text = "Qual é a capital da França?"
        self.assertFalse(assistant._handle_text(text))
        assistant.music.parse.assert_called_once_with(text)
        assistant.commands.parse.assert_called_once_with(text)
        assistant._ask_gemini.assert_called_once_with(text)
        assistant._reply.assert_not_called()

    def test_refresh_includes_completed_tasks(self):
        assistant = self.assistant()
        item = assistant.productivity.add_task("Enviar relatório")
        assistant.productivity.complete_task(item["id"])
        assistant.refresh_productivity()
        self.assertEqual(assistant.snapshots, [assistant.productivity.list_items(True)])
        self.assertEqual(len(assistant.snapshots[0]), 1)

    def test_due_alert_during_conversation_preserves_audio_state_and_turn(self):
        assistant = self.assistant()
        item = assistant.productivity.add_timer(5, "Café")
        assistant.state = State.SPEAKING
        turn = assistant._turn
        turn.mark("first_token", 123.0)
        marks = dict(turn.marks)
        self.now += 5
        assistant._lock.acquire()
        try:
            assistant._poll_productivity()
            assistant._poll_productivity()
        finally:
            assistant._lock.release()
        self.assertEqual(len(assistant.alerts), 1)
        self.assertEqual(assistant.alerts[0]["id"], item["id"])
        self.assertTrue(assistant.events)
        self.assertTrue(all(role == "system" for role, _ in assistant.events))
        self.assertEqual(len(assistant.snapshots), 1)
        self.assertEqual(assistant.state, State.SPEAKING)
        self.assertIs(assistant._turn, turn)
        self.assertEqual(turn.marks, marks)
        self.assertEqual(assistant.states, [])
        assistant._reply.assert_not_called()
        assistant._fail.assert_not_called()
        self.assertEqual(assistant.tts.mock_calls, [])

    def test_storage_error_never_confirms_success_or_falls_through(self):
        service = Mock(spec=ProductivityService)
        service.handle.side_effect = ProductivityError("Não consegui salvar a tarefa no armazenamento local.")
        assistant = self.assistant(service)
        self.assertFalse(assistant._handle_text("adicionar tarefa Comprar pão"))
        service.handle.assert_called_once()
        assistant.music.parse.assert_not_called()
        assistant.whatsapp.handle.assert_not_called()
        assistant._ask_gemini.assert_not_called()
        outputs = [text for _, text in assistant.events]
        outputs += [call.args[0] for call in assistant._reply.call_args_list]
        outputs += [call.args[0] for call in assistant._fail.call_args_list]
        self.assertTrue(any("não consegui" in text.lower() for text in outputs))
        self.assertFalse(any("tarefa adicionada" in text.lower() for text in outputs))
        self.assertEqual(assistant.snapshots, [])

    def test_scheduler_storage_error_does_not_fabricate_alert_or_touch_tts(self):
        service = Mock(spec=ProductivityService)
        service.poll_due.side_effect = ProductivityError("Não consegui consultar os lembretes.")
        assistant = self.assistant(service)
        assistant.state = State.PROCESSING
        assistant._poll_productivity()
        self.assertEqual(assistant.alerts, [])
        self.assertEqual(assistant.snapshots, [])
        self.assertEqual(assistant.state, State.PROCESSING)
        self.assertEqual(assistant.tts.mock_calls, [])

    def test_scheduler_waits_one_second_and_stops_without_an_extra_poll(self):
        assistant = self.assistant(Mock(spec=ProductivityService))
        assistant._quit = Mock()
        assistant._quit.wait.side_effect = [False, True]
        assistant._poll_productivity = Mock()
        assistant._productivity_loop()
        self.assertEqual(assistant._poll_productivity.call_count, 2)  # boot e primeiro intervalo
        self.assertEqual([call.args for call in assistant._quit.wait.call_args_list], [(1,), (1,)])

    def test_boot_starts_one_scheduler_even_when_called_twice(self):
        assistant = self.assistant(Mock(spec=ProductivityService))
        with patch.object(assistant_module.threading, "Thread") as thread_factory:
            assistant.boot()
            assistant.boot()
        scheduler_calls = [call for call in thread_factory.call_args_list
                           if call.kwargs.get("target") == assistant._productivity_loop]
        self.assertEqual(len(scheduler_calls), 1)
        self.assertTrue(scheduler_calls[0].kwargs["daemon"])

    def test_poll_after_shutdown_has_no_calls_or_signals(self):
        service = Mock(spec=ProductivityService)
        assistant = self.assistant(service)
        assistant._quit.set()
        assistant._poll_productivity()
        service.poll_due.assert_not_called()
        self.assertEqual((assistant.events, assistant.snapshots, assistant.alerts), ([], [], []))

    def test_shutdown_started_during_poll_suppresses_result_signals(self):
        service = Mock(spec=ProductivityService)
        assistant = self.assistant(service)
        def poll():
            assistant._quit.set()
            return [{"id": 7, "kind": "timer", "title": "Café"}]
        service.poll_due.side_effect = poll
        assistant._poll_productivity()
        self.assertEqual((assistant.events, assistant.snapshots, assistant.alerts), ([], [], []))
        service.requeue_due.assert_called_once_with([7])

    def test_claim_interrupted_by_shutdown_is_delivered_once_after_restart(self):
        assistant = self.assistant()
        item = assistant.productivity.add_timer(1, "Aviso no encerramento")
        self.now += 1
        original_poll = assistant.productivity.poll_due
        def interrupted_poll():
            rows = original_poll()
            assistant._quit.set()
            return rows
        with patch.object(assistant.productivity, "poll_due", side_effect=interrupted_poll):
            assistant._poll_productivity()
        self.assertEqual((assistant.events, assistant.snapshots, assistant.alerts), ([], [], []))
        pending = assistant.productivity.list_items()
        self.assertEqual([(row["id"], row["status"]) for row in pending], [(item["id"], "active")])
        restarted = ProductivityService(assistant.productivity.path, now=lambda: self.now)
        self.addCleanup(restarted.close)
        self.assertEqual([row["id"] for row in restarted.poll_due()], [item["id"]])
        self.assertEqual(restarted.poll_due(), [])

    def test_alert_awaiting_ui_acknowledgement_is_requeued_on_shutdown(self):
        assistant = self.assistant()
        item = assistant.productivity.add_timer(1, "Aviso ainda enfileirado")
        self.now += 1
        assistant._poll_productivity()
        self.assertEqual([row["id"] for row in assistant.alerts], [item["id"]])
        assistant.shutdown()
        restarted = ProductivityService(assistant.productivity.path, now=lambda: self.now)
        self.addCleanup(restarted.close)
        self.assertEqual([(row["id"], row["status"]) for row in restarted.list_items()],
                         [(item["id"], "active")])
        self.assertEqual([row["id"] for row in restarted.poll_due()], [item["id"]])
        self.assertEqual(restarted.poll_due(), [])

    def test_acknowledged_alert_is_not_requeued_on_shutdown(self):
        assistant = self.assistant()
        item = assistant.productivity.add_timer(1, "Aviso exibido")
        self.now += 1
        assistant._poll_productivity()
        assistant.acknowledge_productivity_alert(item["id"])
        assistant.shutdown()
        restarted = ProductivityService(assistant.productivity.path, now=lambda: self.now)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.list_items(), [])
        self.assertEqual(restarted.list_items(True)[0]["status"], "fired")
        self.assertEqual(restarted.poll_due(), [])

    def test_shutdown_joins_scheduler_before_closing_storage(self):
        service = Mock(spec=ProductivityService)
        assistant = self.assistant(service)
        assistant.stop_voice_test = Mock()
        order = []
        thread = Mock()
        def join(*args, **kwargs):
            self.assertTrue(assistant._quit.is_set())
            order.append("joined")
        thread.join.side_effect = join
        assistant._productivity_thread = thread
        service.close.side_effect = lambda: order.append("closed")
        assistant.shutdown()
        thread.join.assert_called_once()
        service.close.assert_called_once_with()
        self.assertEqual(order, ["joined", "closed"])


if __name__ == "__main__":
    unittest.main()
