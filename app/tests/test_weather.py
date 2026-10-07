"""python -m app.tests.test_weather  — busca o clima da cidade configurada."""
import sys

from app.core.config import Settings
from app.services.weather_service import WeatherError, WeatherService


def main() -> None:
    s = Settings.load()
    try:
        d = WeatherService(s).fetch(force=True)
    except WeatherError as e:
        sys.exit(f"ERRO: {e}")
    print(WeatherService.card_text(d))
    print("\nFala:", WeatherService.spoken(d))


if __name__ == "__main__":
    main()
