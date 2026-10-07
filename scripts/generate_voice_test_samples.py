"""Gera frases públicas sintéticas para avaliar ativação, sem usar microfone."""
import asyncio
from pathlib import Path
import edge_tts

SAMPLES = {
    "wake": "Apolo",
    "inline": "Apolo, que horas são?",
    "command": "Apolo, abra a calculadora.",
    "courtesy": "Ei Apolo, como está o tempo?",
    "negative_name": "Travis",
    "negative_command": "Abra a calculadora.",
    "negative_mention": "Eu gosto do Apolo.",
    "negative_conversation": "Hoje o dia está muito bonito.",
}


async def main():
    output = Path(__file__).resolve().parents[1] / "validacao" / "voice_samples"
    output.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(3)

    async def generate(name, phrase, voice):
        async with semaphore:
            file = output / f"{voice}-{name}.mp3"
            if not file.exists():
                await edge_tts.Communicate(phrase, voice).save(str(file))
            print(file.name, flush=True)

    await asyncio.gather(*(generate(name, phrase, voice) for voice in
                           ("pt-BR-AntonioNeural", "pt-BR-FranciscaNeural") for name, phrase in SAMPLES.items()))


if __name__ == "__main__":
    asyncio.run(main())
