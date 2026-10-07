"""Regressões de chamadas curtas, transição e ativação contínua sem hardware."""
import threading
import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np

from app.tests import test_flow
from app.core.config import Settings
from app.services.audio_engine import AudioEngine
from app.services.speech_service import SpeechService, UtteranceCapture, FRAME
from app.services.wake_word import Heard, WakeInfo, WakeWordDetector, WhisperWakeSTT, match_wake
from app.services.keyword_spotter import StreamingKeywordSpotter


class VoiceUpgradeTests(unittest.TestCase):
    def detector(self, transcribe, callback):
        detector = WakeWordDetector(Settings(wake_prefix_s=0, wake_soft_silence_ms=0), transcribe, callback)
        detector.ready = True
        factory = detector._new_capture
        def capture():
            cap = factory()
            cap._is_speech = lambda frame: bool(frame[0])
            return cap
        detector._new_capture = capture
        self.addCleanup(detector.shutdown)
        return detector

    @staticmethod
    def feed(detector, count, amplitude):
        for _ in range(count):
            detector.feed(np.full(FRAME, amplitude, dtype=np.float32))

    def test_brief_keyword_is_not_discarded_as_noise(self):
        called = []
        detector = self.detector(lambda pcm: Heard('Apolo'), lambda *args: called.append(args))
        self.feed(detector, 8, .1)  # 160 ms de fala real
        self.feed(detector, 20, 0)
        detector.flush()
        self.assertEqual(len(called), 1)

    def test_completed_command_survives_slow_keyword_recognition(self):
        entered, release = threading.Event(), threading.Event()
        carry = []
        calls = []
        def transcribe(pcm):
            calls.append(pcm)
            if len(calls) == 1:
                entered.set()
                release.wait(2)
                return Heard('Apolo')
            return Heard('abra a calculadora')
        detector = None
        def wake(*args):
            carry.append(detector.take_carry())
            return True
        detector = self.detector(transcribe, wake)
        self.feed(detector, 15, .1)
        self.feed(detector, 20, 0)
        self.assertTrue(entered.wait(1))
        try:
            self.feed(detector, 45, .2)
            self.feed(detector, 20, 0)  # pedido INTEIRO terminou antes da detecção
        finally:
            release.set()
        detector.flush()
        self.assertEqual(len(carry), 1)
        self.assertTrue(carry[0].speech)
        values = np.frombuffer(carry[0].pcm, dtype=np.int16)
        self.assertEqual(np.count_nonzero(values > 5000), 45 * FRAME)
        self.assertFalse(np.any((values > 0) & (values < 5000)))  # chamada não foi duplicada

    def test_activation_listen_keeps_queued_audio_without_seed(self):
        assistant, _ = test_flow.build('que horas são?', [])
        self.addCleanup(assistant.wake.shutdown)
        with patch.object(assistant.audio, 'flush') as flush:
            with patch.object(assistant.speech, 'listen', return_value='que horas são?') as listen:
                self.assertEqual(assistant._listen(activation=True), 'que horas são?')
                flush.assert_not_called()
                self.assertEqual(listen.call_args.kwargs['seed'], b'')

    def test_capture_is_installed_before_activation_feedback(self):
        class Engine(AudioEngine):
            ok = True
        engine = Engine()
        service = SpeechService(Settings(stt_speculative=False))
        factory = service._new_capture
        def capture(**kwargs):
            cap = factory(**kwargs)
            cap._is_speech = lambda frame: bool(frame[0])
            return cap
        service._new_capture = capture
        service.transcribe = lambda pcm, stats=None: 'que horas são?'
        def feedback():
            self.assertIsNotNone(engine._sink)
            for _ in range(30):
                engine._sink(np.full(FRAME, .2, dtype=np.float32))
            for _ in range(45):
                engine._sink(np.zeros(FRAME, dtype=np.float32))
        self.assertEqual(service.listen(engine, activation=True, seed=b'', on_ready=feedback), 'que horas são?')

    def test_feedback_is_skipped_when_user_already_started(self):
        class Engine(AudioEngine):
            ok = True
        service = SpeechService(Settings(stt_speculative=False))
        voice = np.full(FRAME, 3000, dtype=np.int16).tobytes()
        quiet = np.zeros(FRAME, dtype=np.int16).tobytes()
        factory = service._new_capture
        def capture(**kwargs):
            cap = factory(**kwargs)
            cap._is_speech = lambda frame: bool(frame[0])
            return cap
        service._new_capture = capture
        service.transcribe = lambda pcm, stats=None: 'abra a calculadora'
        with patch('builtins.print') as feedback:
            self.assertEqual(service.listen(Engine(), activation=True, seed=voice*30+quiet*45, on_ready=feedback), 'abra a calculadora')
            feedback.assert_not_called()

    def test_whisper_receives_normalized_and_padded_short_word(self):
        service = WhisperWakeSTT(Settings())
        service._model = object()
        source = (np.sin(np.arange(3200)*.07)*160).astype(np.int16)
        received = []
        def infer(audio, **kwargs):
            received.append(audio)
            return Heard('Apolo')
        service._infer = infer
        self.assertEqual(service(source.tobytes()).text, 'Apolo')
        self.assertEqual(len(received[0]), 3200+6400)
        self.assertGreater(np.max(np.abs(received[0])), np.max(np.abs(source))/32768)
        self.assertLessEqual(np.max(np.abs(received[0])), .95)

    def test_decoder_silence_result_cannot_activate(self):
        called = []
        detector = self.detector(lambda pcm: '', lambda *args: called.append(args))
        detector._handle(Heard('Apolo', 1, .95))
        self.assertEqual(called, [])

    def test_keyword_aliases_do_not_override_custom_wake_word(self):
        self.assertFalse(match_wake('Apollo', wake_word='atlas').found)

    def test_stale_streaming_result_cannot_activate_after_reset(self):
        called = []
        detector = self.detector(lambda pcm: '', lambda *args: called.append(args))
        self.feed(detector, 15, .1)
        capture, epoch = detector._cap, detector._epoch
        detector.reset()
        detector._stream_hit(capture, epoch)
        self.assertEqual(called, [])

    def test_streaming_hit_preserves_full_utterance(self):
        carry = []
        detector = None
        def wake(*args):
            carry.append(detector.take_carry())
            return True
        detector = self.detector(lambda pcm: '', wake)
        self.feed(detector, 20, .1)
        self.feed(detector, 20, .2)
        detector._stream_hit(detector._cap, detector._epoch)
        self.assertEqual(len(carry), 1)
        self.assertEqual(len(carry[0].pcm), 40 * FRAME * 2)
        self.assertTrue(detector.muted)

    def test_keyword_event_is_read_once_with_its_timestamps(self):
        service = StreamingKeywordSpotter(Settings())
        pending = [True]
        reads, called = [], []
        def result(stream):
            reads.append(1)
            return SimpleNamespace(keyword='Apolo' if len(reads) == 1 else '', timestamps=[.2] if len(reads) == 1 else [])
        service._spotter = SimpleNamespace(create_stream=lambda: SimpleNamespace(accept_waveform=lambda *args: None),
                                          is_ready=lambda s: pending[0], decode_stream=lambda s: pending.__setitem__(0, False),
                                          keyword_spotter=SimpleNamespace(get_result=result), reset_stream=lambda s: None)
        service.ready = True
        cap = SimpleNamespace(wake_capture_id=1)
        def wake(*args):
            called.append(args)
            service.stop()
        service.feed(np.zeros(FRAME, dtype=np.float32), cap, 0, wake)
        worker = threading.Thread(target=service._run, daemon=True)
        worker.start()
        worker.join(1)
        self.assertEqual(reads, [1])
        self.assertEqual(called, [(cap, 0)])
        self.assertFalse(worker.is_alive())

    def test_voice_diagnostic_does_not_execute_commands(self):
        assistant, _ = test_flow.build('', [])
        self.addCleanup(assistant.wake.shutdown)
        assistant.wake.ready = True
        self.assertTrue(assistant.start_voice_test())
        with patch.object(assistant, 'activate_apolo') as activate:
            self.assertTrue(assistant._on_wake('abra a calculadora', 'Apolo, abra a calculadora'))
            activate.assert_not_called()
        epoch = assistant.wake._epoch
        assistant.stop_voice_test()
        self.assertGreater(assistant.wake._epoch, epoch)
        self.assertFalse(assistant._voice_test_active)

    def test_new_defaults_migrate_without_replacing_custom_models(self):
        from app.core import config
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'settings.json'
            file.write_text(json.dumps(dict(config_version=3, wake_model='tiny', wake_prefix_s=1.0)))
            with patch.object(config, 'SETTINGS_FILE', file):
                cfg = Settings.load()
                self.assertEqual((cfg.wake_model, cfg.wake_engine, cfg.wake_prefix_s), ('base', 'hybrid', .7))
                file.write_text(json.dumps(dict(config_version=3, wake_model='small', wake_prefix_s=1.5)))
                self.assertEqual((Settings.load().wake_model, Settings.load().wake_prefix_s), ('small', 1.5))

    def test_inline_command_is_reassembled_when_more_speech_arrives(self):
        release, entered = threading.Event(), threading.Event()
        called, carried = [], []
        def recognize(pcm):
            entered.set()
            release.wait(2)
            return Heard('Apolo, abra')
        detector = None
        def wake(command, text, info):
            called.append((command, info.partial))
            carried.append(detector.take_carry())
            return True
        detector = self.detector(recognize, wake)
        self.feed(detector, 25, .1)
        self.feed(detector, 20, 0)
        self.assertTrue(entered.wait(1))
        self.feed(detector, 30, .2)
        release.set()
        detector.flush()
        self.assertEqual(called, [('', True)])
        samples = np.frombuffer(carried[0].pcm, dtype=np.int16)
        self.assertEqual(np.count_nonzero(samples > 0), 55 * FRAME)

    def test_post_response_rearm_does_not_impose_full_cooldown(self):
        calls = []
        detector = self.detector(lambda pcm: '', lambda *args: calls.append(args))
        clock = [10.0]
        detector.clock = lambda: clock[0]
        detector._handle(Heard('Apolo'))
        self.assertEqual(len(calls), 1)
        detector.reset(rearm=True)
        clock[0] += .4
        detector._handle(Heard('Apolo'))
        self.assertEqual(len(calls), 2)

    def test_voice_activation_does_not_speak_into_its_command_capture(self):
        assistant, _ = test_flow.build('', [])
        self.addCleanup(assistant.wake.shutdown)
        assistant.cfg.activation_feedback = 'voice'
        with patch.object(assistant.activation, 'pick') as pick, patch.object(assistant.tts, 'play_pcm') as play:
            assistant._play_voice_activation_cue()
            pick.assert_not_called()
            self.assertEqual(len(play.call_args.args[0]), 2160)


if __name__ == '__main__':
    unittest.main()
