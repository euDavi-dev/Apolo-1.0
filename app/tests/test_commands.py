"""python -m app.tests.test_commands — testa o parser de comandos (nada é executado)."""
import sys

from app.services import intent_service
from app.services.command_service import CommandService, is_yes

CASES = [
    ("APOLO, abra o Chrome.", "chrome"), ("abra o youtube", "youtube"), ("Abra a calculadora", "calculator"),
    ("abra o VS Code", "vscode"), ("por favor abra o bloco de notas", "notepad"),
    ("desligue o computador", "shutdown"), ("reinicie o PC", "restart"),
    ("cancele o desligamento", "cancel_shutdown"), ("bloqueie o computador", "lock"),
    ("pesquise gatos engraçados no youtube", "search_youtube"),
    ("abra o Excel", "unknown"),                      # fora da lista permitida
    ("apague todos os meus arquivos", None),          # nunca vira comando
    ("qual a capital da França?", None), ("você pode me explicar o que é um buraco negro", None),
]


def main() -> None:
    svc, bad = CommandService(), 0
    for text, want in CASES:
        m = svc.parse(text)
        got = m[0].id if m else None
        ok = got == want
        bad += not ok
        print(f"[{'OK' if ok else 'FALHOU'}] {text!r} -> {got}")
    for text, want in [("Que horas são?", "time"), ("que dia é hoje", "date"), ("Como está o clima?", "weather"),
                       ("qual a temperatura hoje", "weather"), ("conte uma piada", None)]:
        got = intent_service.detect(text)
        bad += got != want
        print(f"[{'OK' if got == want else 'FALHOU'}] intenção {text!r} -> {got}")
    for text, want in [("sim", True), ("pode confirmar", True), ("não", False), ("não, cancela", False)]:
        bad += is_yes(text) != want
        print(f"[{'OK' if is_yes(text) == want else 'FALHOU'}] confirmação {text!r} -> {is_yes(text)}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
