"""Clima via Open-Meteo (gratuito, sem chave de API)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from app.core.config import Settings

log = logging.getLogger("apolo.weather")
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
CACHE_S = 600

WMO = {0: "céu limpo", 1: "predominantemente limpo", 2: "parcialmente nublado", 3: "nublado",
       45: "neblina", 48: "neblina", 51: "garoa fraca", 53: "garoa", 55: "garoa intensa",
       56: "garoa congelante", 57: "garoa congelante", 61: "chuva fraca", 63: "chuva", 65: "chuva forte",
       66: "chuva congelante", 67: "chuva congelante", 71: "neve fraca", 73: "neve", 75: "neve forte",
       77: "grãos de neve", 80: "pancadas de chuva fracas", 81: "pancadas de chuva",
       82: "pancadas de chuva fortes", 85: "pancadas de neve", 86: "pancadas de neve fortes",
       95: "trovoadas", 96: "trovoadas com granizo", 99: "trovoadas com granizo forte"}

UF = {"Acre": "AC", "Alagoas": "AL", "Amapá": "AP", "Amazonas": "AM", "Bahia": "BA", "Ceará": "CE",
      "Distrito Federal": "DF", "Espírito Santo": "ES", "Goiás": "GO", "Maranhão": "MA",
      "Mato Grosso": "MT", "Mato Grosso do Sul": "MS", "Minas Gerais": "MG", "Pará": "PA",
      "Paraíba": "PB", "Paraná": "PR", "Pernambuco": "PE", "Piauí": "PI", "Rio de Janeiro": "RJ",
      "Rio Grande do Norte": "RN", "Rio Grande do Sul": "RS", "Rondônia": "RO", "Roraima": "RR",
      "Santa Catarina": "SC", "São Paulo": "SP", "Sergipe": "SE", "Tocantins": "TO"}


class WeatherError(Exception):
    pass


@dataclass
class WeatherData:
    city: str
    region: str
    temp: int
    feels_like: int
    condition: str
    humidity: int
    wind: int
    tmax: int
    tmin: int
    rain_prob: int
    unit: str  # "C" | "F"

    @property
    def place(self) -> str:
        return f"{self.city}, {self.region}" if self.region else self.city


class WeatherService:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self._geo: dict = {}
        self._cache: tuple[float, tuple, WeatherData] | None = None

    def _key(self) -> tuple:
        c = self.cfg
        return (c.weather_city, c.weather_country, c.weather_unit)

    def _locate(self) -> dict:
        key = self._key()[:2]
        if self._geo.get("key") == key:
            return self._geo
        name, _, hint = self.cfg.weather_city.partition(",")
        params = {"name": name.strip(), "count": 10, "language": "pt", "format": "json"}
        if self.cfg.weather_country.strip():
            params["countryCode"] = self.cfg.weather_country.strip().upper()
        try:
            res = requests.get(GEOCODE, params=params, timeout=8).json().get("results") or []
        except requests.RequestException as e:
            raise WeatherError("Sem conexão com o serviço de clima.") from e
        if not res:
            raise WeatherError(f"Não encontrei a cidade {name.strip()}.")
        hint = hint.strip().lower()
        pick = res[0]
        if hint:
            for r in res:
                adm = r.get("admin1", "")
                if hint in (adm.lower(), UF.get(adm, "").lower()):
                    pick = r
                    break
        adm = pick.get("admin1", "")
        self._geo = {"key": key, "lat": pick["latitude"], "lon": pick["longitude"],
                     "city": pick["name"], "region": UF.get(adm, adm) if pick.get("country_code") == "BR" else adm}
        return self._geo

    def fetch(self, force: bool = False) -> WeatherData:
        now = time.time()
        if not force and self._cache and self._cache[1] == self._key() and now - self._cache[0] < CACHE_S:
            return self._cache[2]
        geo = self._locate()
        fahrenheit = self.cfg.weather_unit == "fahrenheit"
        params = {
            "latitude": geo["lat"], "longitude": geo["lon"], "timezone": "auto", "forecast_days": 1,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "temperature_unit": "fahrenheit" if fahrenheit else "celsius",
        }
        try:
            r = requests.get(FORECAST, params=params, timeout=8)
            r.raise_for_status()
            j = r.json()
            cur, day = j["current"], j["daily"]
            data = WeatherData(
                city=geo["city"], region=geo["region"],
                temp=round(cur["temperature_2m"]), feels_like=round(cur["apparent_temperature"]),
                condition=WMO.get(cur["weather_code"], "condições variáveis"),
                humidity=round(cur["relative_humidity_2m"]), wind=round(cur["wind_speed_10m"]),
                tmax=round(day["temperature_2m_max"][0]), tmin=round(day["temperature_2m_min"][0]),
                rain_prob=round(day["precipitation_probability_max"][0] or 0),
                unit="F" if fahrenheit else "C")
        except requests.RequestException as e:
            raise WeatherError("O serviço de clima está indisponível no momento.") from e
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise WeatherError("Resposta inesperada do serviço de clima.") from e
        self._cache = (now, self._key(), data)
        return data

    @staticmethod
    def spoken(d: WeatherData) -> str:
        unit = "graus Fahrenheit" if d.unit == "F" else "graus"
        return (f"Em {d.city}, agora faz {d.temp} {unit}, {d.condition}. "
                f"Sensação térmica de {d.feels_like}. Umidade de {d.humidity} por cento e vento de "
                f"{d.wind} quilômetros por hora. Hoje a máxima é {d.tmax} e a mínima {d.tmin}, "
                f"com {d.rain_prob} por cento de chance de chuva.")

    @staticmethod
    def card_text(d: WeatherData) -> str:
        return (f"{d.place.upper()}\n\n{d.temp}°{d.unit}\n{d.condition.capitalize()}\n\n"
                f"Umidade: {d.humidity}%\nVento: {d.wind} km/h")
