"""Regressões de latência, sem modelo, rede ou microfone."""
import unittest
import threading
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import numpy as np
from app.tests import test_flow  # stubs para execução offline
from app.core.config import Settings
from app.services.speech_service import UtteranceCapture, FRAME
from app.services.wake_word import WakeWordDetector, Heard, Carry, WakeInfo

class WakeLatencyTests(unittest.TestCase):
    def test_boot_does_not_wait_for_network_checks(self):
        assistant, _ = test_flow.build('', [])
        self.addCleanup(assistant.wake.shutdown)
        release = threading.Event()
        assistant._check_ai = lambda: (release.wait(2), '')[1]
        assistant._refresh_weather = lambda force: (release.wait(2), True)[1]
        assistant.cfg.wake_word_enabled = False
        assistant.cfg.music_enabled = False
        worker = threading.Thread(target=assistant._boot, daemon=True)
        worker.start()
        worker.join(1)
        try:
            self.assertFalse(worker.is_alive(), 'a inicialização esperou a rede')
        finally:
            assistant._quit.set()
            release.set()
    def test_tiny_unavailable_uses_only_cached_base(self):
        from app.services.wake_word import WhisperWakeSTT
        calls = []
        def load(name, **kwargs):
            calls.append((name, kwargs))
            if name == 'tiny':
                raise OSError('offline')
            return object()
        service = WhisperWakeSTT(Settings(wake_model='tiny'))
        service._infer = lambda audio: Heard('')
        with patch('faster_whisper.WhisperModel', side_effect=load):
            self.assertTrue(service.load())
        self.assertEqual(service.effective_model, 'base')
        self.assertTrue(calls[1][1]['local_files_only'])

    def detector(self, transcribe, callback=lambda *args: True, **settings):
        detector = WakeWordDetector(Settings(**settings), transcribe, callback)
        detector.ready = True
        factory = detector._new_capture
        def capture():
            cap = factory()
            cap._is_speech = lambda frame: bool(np.max(np.abs(frame)) > 100)
            return cap
        detector._new_capture = capture
        self.addCleanup(detector.shutdown)
        return detector

    @staticmethod
    def feed(detector, frames, value=.1):
        for _ in range(frames):
            detector.feed(np.full(FRAME, value, dtype=np.float32))

    def test_old_recognition_cannot_activate_after_reset(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def recognize(pcm):
            entered.set()
            release.wait(2)
            return Heard('Apolo')
        detector = self.detector(recognize, lambda *args: calls.append(args), wake_prefix_s=0)
        self.feed(detector, 30)
        self.feed(detector, 25, 0)
        self.assertTrue(entered.wait(1))
        detector.reset()
        release.set()
        detector.flush()
        self.assertEqual(calls, [])

    def test_no_prefix_transcription_while_music_plays(self):
        transcribed = []
        detector = self.detector(lambda pcm: transcribed.append(pcm) or Heard('Apolo'))
        detector.noisy = lambda: True
        self.feed(detector, 200)
        self.feed(detector, 25, 0)
        detector.flush()
        self.assertEqual(transcribed, [])
        self.assertEqual(detector.stats['music_skipped'], 1)

    def test_unrelated_sentence_does_not_activate(self):
        calls = []
        detector = self.detector(lambda pcm: Heard('Eu gosto do Apolo'), lambda *args: calls.append(args))
        self.feed(detector, 70)
        detector.flush()
        self.assertEqual(calls, [])

    def test_prefix_callback_can_take_audio_without_holding_vad_lock(self):
        completed = threading.Event()
        calls = []
        detector = None
        def wake(*args):
            # Outra thread consegue acessar o VAD durante o callback (ordem de travas do AudioEngine).
            thread = threading.Thread(target=lambda: (detector.take_carry(), completed.set()), daemon=True)
            thread.start()
            calls.append(completed.wait(1))
            return True
        detector = self.detector(lambda pcm: Heard('Apolo, abra'), wake)
        self.feed(detector, 60)
        detector.flush()
        self.assertEqual(calls, [True])

    def test_audio_survives_sentence_end_during_handoff(self):
        carried = []
        detector = None
        def wake(*args):
            # Simula áudio chegando enquanto o Assistant prepara a troca do sink.
            self.feed(detector, 20, 0)
            self.feed(detector, 30, .2)
            carried.append(detector.take_carry())
            return True
        detector = self.detector(lambda pcm: Heard('Apolo abra'), wake)
        self.feed(detector, 50)
        detector.flush()
        self.assertEqual(len(carried), 1)
        self.assertGreaterEqual(len(carried[0].pcm), 80 * FRAME * 2)
        samples = np.frombuffer(carried[0].pcm, dtype=np.int16)
        self.assertLess(samples[0], samples[-1])  # nome e comando estão presentes, em ordem

    def test_low_energy_noise_cannot_keep_recording_open(self):
        class AlwaysSpeech:
            def is_speech(self, pcm, rate): return True
        cap = UtteranceCapture(end_silence_ms=300)
        cap.vad = AlwaysSpeech()
        for _ in range(30): cap.feed(np.full(FRAME, .1, dtype=np.float32))
        for _ in range(20): cap.feed(np.full(FRAME, .001, dtype=np.float32))
        self.assertTrue(cap.done.is_set())
        self.assertEqual(cap.end_reason, 'silêncio')

    def test_migration_updates_old_defaults_but_keeps_custom_values(self):
        from app.core import config
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_text(json.dumps({'config_version': 2, 'wake_model': 'base',
                                        'wake_end_silence_ms': 450, 'weather_city': 'Recife'}))
            with patch.object(config, 'SETTINGS_FILE', path):
                cfg = Settings.load()
                self.assertEqual((cfg.wake_model, cfg.wake_end_silence_ms, cfg.weather_city), ('base', 300, 'Recife'))
                path.write_text(json.dumps({'config_version': 2, 'wake_model': 'small', 'wake_end_silence_ms': 650}))
                cfg = Settings.load()
                self.assertEqual((cfg.wake_model, cfg.wake_end_silence_ms), ('small', 650))

    def test_early_keyword_only_waits_for_command(self):
        assistant, _ = test_flow.build('unused', ['Não deveria chamar a IA.'])
        self.addCleanup(assistant.wake.shutdown)
        texts = iter(['Apolo', 'Que horas são?'])
        assistant.speech.listen = lambda *args, **kwargs: next(texts)
        assistant.cfg.activation_feedback = 'silent'
        assistant._lock.acquire()
        assistant._session('voice', 'voice', '', carry=Carry(b'\x00\x00', True), info=WakeInfo(partial=True))
        self.assertTrue(assistant.tts.spoken)
        self.assertNotIn('Não deveria', assistant.tts.spoken[0])
        self.assertFalse(assistant._lock.locked())

    def test_tone_is_short_and_voice_can_be_disabled(self):
        from app.utils.sounds import activation_pcm
        pcm = activation_pcm()
        self.assertEqual(len(pcm), 2160)
        self.assertEqual(pcm.dtype, np.int16)
        assistant, _ = test_flow.build('', [])
        self.addCleanup(assistant.wake.shutdown)
        played = []
        assistant.tts.play_pcm = lambda pcm: played.append(len(pcm))
        assistant._play_activation_response()
        assistant.cfg.activation_feedback = 'silent'
        assistant._play_activation_response()
        self.assertEqual(played, [2160])

    def test_limit_counts_recording_not_idle(self):
        cap = UtteranceCapture(max_s=1.0)
        cap._is_speech = lambda frame: bool(frame[0])
        for _ in range(150):
            cap.feed_int16(np.zeros(FRAME, dtype=np.int16).tobytes())
        voice = np.full(FRAME, 1000, dtype=np.int16).tobytes()
        for _ in range(10):
            cap.feed_int16(voice)
        self.assertTrue(cap.triggered)
        self.assertFalse(cap.done.is_set())
        for _ in range(50):
            cap.feed_int16(voice)
        self.assertTrue(cap.done.is_set())

    def test_continuous_command_activates_before_end_and_keeps_audio(self):
        calls, carry = [], []
        detected = threading.Event()
        detector = None
        def wake(command, text, info):
            calls.append(command)
            carry.append(detector.take_carry())
            detected.set()
            return True
        detector = WakeWordDetector(Settings(), lambda pcm: Heard('Apolo toque Black'), wake)
        detector.ready = True
        detector._cap = detector._new_capture()
        detector._cap._is_speech = lambda frame: True
        try:
            for _ in range(82):
                detector.feed(np.full(FRAME, .1, dtype=np.float32))
            self.assertTrue(detected.wait(2))
            self.assertEqual(calls, [''])
            self.assertTrue(carry[0].speech)
            self.assertGreaterEqual(len(carry[0].pcm), int(detector.cfg.wake_prefix_s / .02) * FRAME * 2)
        finally:
            detector.shutdown()

if __name__ == '__main__':
    unittest.main()
