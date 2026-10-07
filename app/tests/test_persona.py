"""python -m app.tests.test_persona --live  — manda perguntas de exemplo ao Gemini e mostra como o APOLO responde
(use para avaliar a personalidade; precisa de chave e internet). Sem --live, só mostra o prompt e a linha dinâmica."""
import sys

from app.core.config import Settings
from app.core.persona import SYSTEM_PROMPT
from app.services.gemini_service import GeminiError, GeminiService
from app.utils.textutils import clean_for_tts

PROMPTS = [
    "APOLO, que horas são? Já passa da meia-noite.",
    "Qual a capital do Brasil?",
    "Eu fiz uma besteira.",
    "Explique de forma simples como funciona um buraco negro.",
    "Quanto é raiz quadrada de 144 mais 8?",
    "Qual foi o resultado do jogo de ontem?",
    "Meu pai está no hospital e estou preocupado.",
    "Acho que a Terra tem 6 mil anos, certo?",
]


def main() -> None:
    if "--live" not in sys.argv:
        print(SYSTEM_PROMPT)
        print("\n(rode com --live para testar com o Gemini)")
        return
    s = Settings.load()
    s.persona_humor = 0.6          # mais alto só para o teste mostrar o estilo; no uso normal é 0,28
    g = GeminiService(s)
    try:
        for q in PROMPTS:
            g.reset()
            ans = "".join(g.stream_reply(q))
            print(f"\nVocê  : {q}\nAPOLO: {clean_for_tts(ans)}")
    except GeminiError as e:
        sys.exit(f"\nERRO ({e.kind}): {e.message}")


if __name__ == "__main__":
    main()
