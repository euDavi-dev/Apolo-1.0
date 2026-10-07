"""Contas e onboarding sem rede, microfone ou dados reais."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from app.core import config
from app.core.accounts import AccountError, AccountStore, activate, credential
from app.ui.account_dialog import AccountDialog, LoginDialog, TutorialWizard


class AccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AccountStore(Path(self.tmp.name))

    def create(self, username="teste"):
        return self.store.create(username, "uma senha forte", "Ana Silva", "Recife")

    def test_encrypted_roundtrip_and_wrong_password(self):
        session = self.create()
        data = dict(session.data, secrets={"GEMINI_API_KEY": "segredo-exclusivo"})
        session.save(data)
        raw = self.store.path("teste").read_text()
        for secret in ("segredo-exclusivo", "uma senha forte", "Ana Silva"):
            self.assertNotIn(secret, raw)
            self.assertNotIn(secret, repr(session))
        self.assertEqual(self.store.login(" TESTE ", "uma senha forte").data, data)
        with self.assertRaises(AccountError):
            self.store.login("teste", "senha errada")

    def test_duplicate_validation_and_tampering(self):
        self.create()
        for username in ("../x", "CON", "abc.", "a", "com1"):
            with self.assertRaises(AccountError):
                self.create(username)
        with self.assertRaises(AccountError):
            self.create("TESTE")
        with self.assertRaises(AccountError):
            self.store.create("outra", "curta", "Ana", "Recife")
        path = self.store.path("teste")
        data = json.loads(path.read_text())
        data["vault"] = "AAAA"
        path.write_text(json.dumps(data))
        with self.assertRaises(AccountError):
            self.store.login("teste", "uma senha forte")

    def test_failed_save_preserves_previous_vault(self):
        session = self.create()
        before = self.store.path("teste").read_bytes()
        with patch("app.core.accounts.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                session.save(dict(session.data, name="Outro nome"))
        self.assertEqual(self.store.path("teste").read_bytes(), before)
        self.assertEqual(session.data["name"], "Ana Silva")
        self.assertFalse(list(self.store.path("teste").parent.glob("*.tmp")))

    def test_profile_isolation_and_no_environment_key_leak(self):
        first, second = self.create(), self.create("outra")
        first.save(dict(first.data, secrets={"GEMINI_API_KEY": "only-first"}))
        with patch.object(config, "DATA_DIR", config.DATA_DIR), \
             patch.object(config, "SETTINGS_FILE", config.SETTINGS_FILE), \
             patch.object(config, "COMMANDS_FILE", config.COMMANDS_FILE), \
             patch.object(config, "ACTIVE_SESSION", None, create=True), \
             patch.dict(os.environ, {"GEMINI_API_KEY": "legacy-key"}):
            activate(first)
            config.Settings(weather_city="Fortaleza").save()
            self.assertEqual(config.api_key(), "only-first")
            activate(second)
            self.assertEqual(config.api_key(), "")
            self.assertFalse(config.SETTINGS_FILE.exists())
            self.assertNotEqual(config.Settings.load().weather_city, "Fortaleza")
            self.assertEqual(config.COMMANDS_FILE.parent, second.directory)

    def test_tutorial_completion_cancel_and_profile_edit(self):
        session = self.create()
        wizard = TutorialWizard(session)
        wizard.profile.name.setText("Novo nome")
        wizard.reject()
        self.assertFalse(self.store.login("teste", "uma senha forte").data["tutorial_done"])
        self.assertEqual(session.data["name"], "Ana Silva")
        wizard = TutorialWizard(session)
        wizard.profile.keys["GEMINI_API_KEY"].setText("api-test")
        wizard.accept()
        self.assertTrue(self.store.login("teste", "uma senha forte").data["tutorial_done"])
        dialog = AccountDialog(session)
        dialog.profile.name.setText("Nome atualizado")
        dialog.profile.keys["GEMINI_API_KEY"].clear()
        dialog.save()
        self.assertEqual(session.data["name"], "Nome atualizado")
        self.assertEqual(session.data["secrets"]["GEMINI_API_KEY"], "")

    def test_registration_and_login_ui(self):
        dialog = LoginDialog(self.store)
        self.assertTrue(dialog.signup)
        dialog.username.setText("ana")
        dialog.password.setText("uma senha forte")
        dialog.confirm.setText("uma senha forte")
        dialog.name.setText("Ana Silva")
        dialog.city.setText("Recife")
        dialog.authenticate()
        self.assertIsNotNone(dialog.session)
        self.assertEqual(dialog.password.text(), "")
        returning = LoginDialog(self.store)
        self.assertFalse(returning.signup)
        returning.username.setText("ana")
        returning.password.setText("uma senha forte")
        returning.authenticate()
        self.assertEqual(returning.session.username, "ana")

    def test_cancel_startup_never_loads_assistant_services(self):
        script = '''
import sys
from unittest.mock import patch
from app import main
assert "app.core.assistant" not in sys.modules
with patch.object(main, "QLockFile") as lock, patch.object(main.LoginDialog, "exec", return_value=0):
    lock.return_value.tryLock.return_value = True
    main.main()
    lock.return_value.unlock.assert_called_once()
assert "app.core.assistant" not in sys.modules
assert "app.services.audio_engine" not in sys.modules
'''
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
