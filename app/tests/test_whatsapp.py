"""Integração por links oficiais: testes sem navegador, conta ou envio."""
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

from app.services.whatsapp_service import WhatsAppError, WhatsAppService, chat_url


class WhatsAppTests(unittest.TestCase):
    def test_phone_and_message_are_encoded(self):
        message = "Olá, mãe! Café às 15:30? & você #sim 😊"
        parsed = urlsplit(chat_url("+55 (11) 99999-9999", message))
        self.assertEqual(parsed.netloc, "wa.me")
        self.assertEqual(parsed.path, "/5511999999999")
        self.assertEqual(parse_qs(parsed.query)["text"], [message])

    def test_invalid_phones_and_message_do_not_open_browser(self):
        opener = Mock(return_value=True)
        svc = WhatsAppService(opener)
        for phone in ("", "0 55 11 99999-9999", "abc5511999999999", "12", "1" * 16,
                      "https://example.com", "55/11999999999", "55;11999999999"):
            with self.subTest(phone=phone), self.assertRaises(WhatsAppError):
                chat_url(phone)
        with self.assertRaises(WhatsAppError):
            svc.open_chat("5511999999999", "a" * 4001)
        with self.assertRaises(WhatsAppError):
            svc.open_chat("", "mensagem")
        opener.assert_not_called()

    def test_voice_commands_preserve_original_message(self):
        opener = Mock(return_value=True)
        svc = WhatsAppService(opener)
        for command in ("Prepare uma mensagem no WhatsApp para +55 11 99999-9999 dizendo Olá, João! Café às 15:30?",
                        "Mensagem no WhatsApp para 5511999999999: Olá, João! Café às 15:30?",
                        "Apolo, por favor prepare mensagem no WhatsApp para 5511999999999 dizendo Olá, João! Café às 15:30?"):
            with self.subTest(command=command):
                self.assertIn("Revise", svc.handle(command))
                url = opener.call_args.args[0]
                self.assertEqual(parse_qs(urlsplit(url).query)["text"], ["Olá, João! Café às 15:30?"])

    def test_web_and_conversation(self):
        opener = Mock(return_value=True)
        svc = WhatsAppService(opener)
        self.assertIn("Web", svc.handle("Abra o WhatsApp"))
        opener.assert_called_with("https://web.whatsapp.com/")
        self.assertIn("conversa", svc.handle("Abra conversa no WhatsApp com +55 11 99999-9999"))
        opener.assert_called_with("https://wa.me/5511999999999")

    def test_failures_are_not_reported_as_success(self):
        for opener in (Mock(return_value=False), Mock(side_effect=OSError("failed"))):
            svc = WhatsAppService(opener)
            self.assertNotIn("Abrindo", svc.handle("abra WhatsApp"))

    def test_general_questions_stay_in_normal_conversation(self):
        svc = WhatsAppService(Mock())
        for text in ("Como funciona o WhatsApp?", "Me lembre de abrir WhatsApp em 5 minutos", "WhatsApp é seguro?"):
            self.assertIsNone(svc.handle(text))
        self.assertIn("DDI", svc.handle("WhatsApp para João"))


if __name__ == "__main__":
    unittest.main()
