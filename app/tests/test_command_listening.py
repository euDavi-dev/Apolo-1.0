"""Regressões da espera após 'Apolo'; sem rede, modelo ou microfone."""
import unittest
from unittest.mock import patch

import numpy as np

from app.tests import test_flow
from app.core.config import Settings
from app.core.state import State
from app.services.audio_engine import AudioEngine
from app.services.speech_service import SpeechError, SpeechService, UtteranceCapture, FRAME


class CommandListeningTests(unittest.TestCase):
    def assistant(self):
        assistant, _ = test_flow.build('', [])
        self.addCleanup(assistant.wake.shutdown)
        assistant._begin_turn('teste')
        return assistant

    def test_unclear_activation_does_not_interrupt_command(self):
        a = self.assistant()
        seeds = []
        def listen(*args, **kwargs):
            seeds.append(kwargs['seed'])
            if len(seeds) == 1:
                raise SpeechError('unclear', 'Não consegui entender, pode repetir?')
            return 'Apolo, que horas são?'
        a.speech.listen = listen
        self.assertEqual(a._listen(b'prefixo', activation=True), 'que horas são?')
        self.assertEqual(seeds, [b'prefixo', b''])
        self.assertEqual(a.tts.spoken, [])

    def test_keyword_then_silence_returns_quietly(self):
        a = self.assistant()
        with patch.object(a.speech, 'listen', side_effect=['Apolo', None]) as listen:
            self.assertIsNone(a._listen(activation=True))
        self.assertEqual(listen.call_count, 2)
        self.assertEqual(a.tts.spoken, [])

    def test_keyword_then_unclear_still_waits_for_command(self):
        a = self.assistant()
        with patch.object(a.speech, 'listen', side_effect=[
                'Apolo', SpeechError('unclear', 'Repita'), 'que horas são?']):
            self.assertEqual(a._listen(activation=True), 'que horas são?')
        self.assertEqual(a.tts.spoken, [])

    def test_network_failure_is_not_hidden(self):
        a = self.assistant()
        with patch.object(a.speech, 'listen', side_effect=SpeechError('network', 'Sem conexão')) as listen:
            with patch.object(a, '_fail') as fail:
                self.assertIsNone(a._listen(activation=True))
                fail.assert_called_once_with('Sem conexão', speak=True)
        self.assertEqual(listen.call_count, 1)

    def test_repeated_unclear_is_bounded(self):
        a = self.assistant()
        with patch.object(a.speech, 'listen', side_effect=SpeechError('unclear', 'Repita')) as listen:
            with patch.object(a, '_fail') as fail:
                self.assertIsNone(a._listen(activation=True))
                fail.assert_called_once()
        self.assertEqual(listen.call_count, 2)

    @staticmethod
    def feed(cap, count, value):
        for _ in range(count):
            cap.feed_int16(np.full(FRAME, value, dtype=np.int16).tobytes())

    def test_short_noise_rearms_and_waits_for_real_command(self):
        cap = UtteranceCapture(start_timeout_s=8, end_silence_ms=800, min_voiced_ms=180)
        cap._is_speech = lambda frame: bool(frame[0])
        self.feed(cap, 6, 1000)
        self.feed(cap, 60, 0)
        self.assertFalse(cap.done.is_set())
        self.assertFalse(cap.triggered)
        self.feed(cap, 25, 1000)
        self.feed(cap, 30, 0)  # pausa de 600 ms no meio do pedido
        self.assertFalse(cap.done.is_set())
        self.feed(cap, 25, 1000)
        self.feed(cap, 40, 0)
        self.assertTrue(cap.done.is_set())
        self.assertEqual(cap.voiced, 50)

    def test_noise_does_not_extend_start_timeout(self):
        cap = UtteranceCapture(start_timeout_s=8, end_silence_ms=800, min_voiced_ms=180)
        cap._is_speech = lambda frame: bool(frame[0])
        self.feed(cap, 6, 1000)
        self.feed(cap, 394, 0)
        self.assertTrue(cap.done.is_set())
        self.assertIsNone(cap.result)

    def test_command_audio_survives_initial_transcription_failure(self):
        class Engine(AudioEngine):
            ok = True
        eng = Engine()
        a = self.assistant()
        a.audio = eng
        a.speech = SpeechService(Settings(stt_speculative=False))
        factory = a.speech._new_capture
        def capture(**kwargs):
            cap = factory(**kwargs)
            cap._is_speech = lambda frame: bool(frame[0])
            return cap
        a.speech._new_capture = capture
        voice = np.full(FRAME, 2000, dtype=np.int16).tobytes()
        quiet = np.zeros(FRAME, dtype=np.int16).tobytes()
        calls = []
        def transcribe(pcm, stats=None):
            calls.append(pcm)
            if len(calls) == 1:
                self.assertTrue(eng._keep_backlog)
                # O microfone entrega o pedido enquanto a primeira transcrição roda.
                eng._backlog.extend([np.full(FRAME, .2, dtype=np.float32) for _ in range(30)])
                eng._backlog.extend([np.zeros(FRAME, dtype=np.float32) for _ in range(45)])
                raise SpeechError('unclear', 'Repita')
            return 'que horas são?'
        a.speech.transcribe = transcribe
        self.assertEqual(a._listen(voice * 30 + quiet * 45, activation=True), 'que horas são?')
        self.assertEqual(len(calls), 2)
        self.assertGreater(np.max(np.frombuffer(calls[1], dtype=np.int16)), 6000)
        self.assertEqual(a.tts.spoken, [])
        self.assertEqual(a.state, State.PROCESSING)

    def test_completed_seed_does_not_swallow_queued_command(self):
        class Engine(AudioEngine):
            ok = True
        eng = Engine()
        svc = SpeechService(Settings(stt_speculative=False))
        factory = svc._new_capture
        def capture(**kwargs):
            cap = factory(**kwargs)
            cap._is_speech = lambda frame: bool(frame[0])
            return cap
        svc._new_capture = capture
        svc.transcribe = lambda pcm, stats=None: 'Apolo'
        voice = np.full(FRAME, 2000, dtype=np.int16).tobytes()
        quiet = np.zeros(FRAME, dtype=np.int16).tobytes()
        eng._backlog = [np.full(FRAME, .3, dtype=np.float32)]
        svc.listen(eng, seed=voice * 30 + quiet * 40 + voice * 10, activation=True)
        # A cauda do seed deve vir antes do áudio que aguardava no motor.
        kept = np.concatenate(eng._backlog)
        self.assertEqual(len(kept), FRAME * 11)
        self.assertAlmostEqual(float(kept[0]), 2000 / 32768, places=4)
        self.assertAlmostEqual(float(kept[-1]), .3, places=4)


if __name__ == '__main__':
    unittest.main()
