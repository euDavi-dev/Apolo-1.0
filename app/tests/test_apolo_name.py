"""Regressões da nova palavra de ativação, sem rede ou microfone."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.core.config import Settings
from app.services.wake_word import match_wake
from app.services import keyword_spotter as kws


class ApoloNameTests(unittest.TestCase):
    def test_call_and_inline_command(self):
        self.assertEqual(Settings().wake_word, "apolo")
        for name in ("Apolo", "APOLO", "Apólo", "Apollo", "A polo", "ei Apolo"):
            with self.subTest(name=name):
                self.assertTrue(match_wake(name).found)
                self.assertEqual(match_wake(name + ", que horas são?").command, "que horas são?")

    def test_old_name_similar_words_and_mentions_do_not_activate(self):
        for text in ("Jarvis", "Jarves", "Garvis", "Jarvis, abra a calculadora",
                     "apoio", "apelo", "polo", "Paulo", "bolo", "rolo",
                     "Eu gosto do Apolo", "O Apolo é legal"):
            with self.subTest(text=text):
                self.assertFalse(match_wake(text).found)

    def test_acoustic_keywords_use_new_pronunciation(self):
        factory = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            fixture_root = Path(temporary) / "fixture"
            model = fixture_root / "models" / "wake" / "keyword"
            model.mkdir(parents=True)
            (model / kws.FILES["lexicon"]).write_text(
                "APOLO AH0 P AA1 L UW0\nJARVIS JH AA1 R V IH0 S\n", encoding="utf-8",
            )
            vocabulary = {"AH0", "P", "AA1", "AO1", "L", "UW0", "OW0", "JH"}
            (model / kws.FILES["tokens"]).write_text(
                "\n".join(f"{phone} {index}" for index, phone in enumerate(sorted(vocabulary))),
                encoding="utf-8",
            )
            with patch.object(kws, "ROOT", fixture_root), \
                 patch.object(kws, "DATA_DIR", Path(temporary) / "data"), \
                 patch.object(kws, "model_valid", side_effect=lambda path: path == model), \
                 patch.object(kws, "prepare_model", side_effect=AssertionError("Teste offline não baixa modelos")) as download, \
                 patch.dict("sys.modules", {"sherpa_onnx": SimpleNamespace(KeywordSpotter=factory)}), \
                 patch.object(kws.threading.Thread, "start"):
                detector = kws.StreamingKeywordSpotter(Settings())
                self.addCleanup(detector.stop)
                self.assertTrue(detector.load(), detector.error)
                keyword_file = Path(factory.call_args.kwargs["keywords_file"])
                text = keyword_file.read_text(encoding="utf-8")
                self.assertEqual(factory.call_args.kwargs["tokens"], str(model / kws.FILES["tokens"]))
                self.assertEqual(text.splitlines(), [
                    "AH0 P AA1 L UW0 @APOLO",  # Pronúncia encontrada no léxico.
                    "AH0 P AO1 L UW0 @APOLO",  # Aproximações do português.
                    "AH0 P AO1 L OW0 @APOLO",
                    "AH0 P AA1 L OW0 @APOLO",
                ])
                for line in text.splitlines():
                    phones, label = line.split(" @")
                    self.assertEqual(label, "APOLO")
                    self.assertTrue(set(phones.split()) <= vocabulary)
                    self.assertNotIn("JH", phones)
                download.assert_not_called()
                detector.stop()


if __name__ == "__main__":
    unittest.main()
