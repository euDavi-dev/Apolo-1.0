"""Configurações: valores padrão < .env < settings.json (editado pela tela de configurações).

No app, chaves são obtidas exclusivamente do cofre da conta ativa."""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("apolo.config")

ROOT = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "APOLO"
SETTINGS_FILE = DATA_DIR / "settings.json"
COMMANDS_FILE = DATA_DIR / "commands.json"
load_dotenv(ROOT / ".env")

DEFAULT_MODEL = "gemini-3.5-flash-lite"

# ---- Ativação (voz + palmas) -------------------------------------------------------------------------------
# Constantes fáceis de editar; os valores viram os PADRÕES de Settings (Configurações > Ativação os sobrescreve).
WAKE_WORD = "apolo"                 # palavra de ativação local: detector contínuo + faster-whisper em português
WAKE_WORD_ENABLED = True             # ativação por voz ligada/desligada (palmas: Settings.clap_enabled)
ACTIVATION_COOLDOWN = 1.5            # s: proteção de repetição; a voz é rearmada com wake_rearm_s após a sessão
WAKE_WORD_COOLDOWN_MS = int(ACTIVATION_COOLDOWN * 1000)   # o mesmo valor em ms (nome pedido); a fonte é ACTIVATION_COOLDOWN
WAKE_WORD_MODEL = "base"             # base prioriza o reconhecimento; o detector contínuo cuida da chamada rápida
                                     # Consome mais recursos que tiny; meça no seu PC com `test_activation --live`.
WAKE_WORD_LOW_LATENCY = True         # True: modelo aquecido na carga, STT especulativo, todas as threads de prontidão,
                                     # guarda de eco adaptativa. False: modo econômico (menos CPU em repouso, mais lento)
WAKE_WORD_MIN_CONFIDENCE = 0.50      # 0..1. confiança = similaridade c/ "apolo" x posição x qualidade do decodificador.
                                     # Um "Apolo" nítido dá ~0.8-1.0; variações duvidosas (Apollo c/ decodificador fraco) ~0.5-0.7.
                                     # Suba (0.7-0.75) se houver ativações falsas; desça (0.5) se ele ignorar você.
WAKE_WORD_PRE_BUFFER_MS = 300        # áudio guardado ANTES do VAD confirmar a fala: a chamada exige 80 ms de voz,
                                     # então 300 ms deixam folga para não cortar a 1ª sílaba de "Apolo".
WAKE_WORD_POST_BUFFER_MS = 200       # silêncio mantido no fim do clip enviado ao STT (ajuda o Whisper a fechar a palavra).
                                     # Precisa ser <= wake_soft_silence_ms, senão a transcrição especulativa nunca é reaproveitada.

# ---- Música ------------------------------------------------------------------------------------------------
MUSIC_ENABLED = True
MUSIC_PROVIDER = "auto"              # "auto" (spotify > youtube > local, o 1º configurado) | "spotify" | "youtube" | "local"
ACTIVATION_DIR = ROOT / "assets" / "sounds" / "activation"   # WAVs pré-renderizados (scripts/generate_activation_audio.py)
ACTIVATION_PHRASES = [               # respostas curtas, faladas na ordem aleatória; NÃO podem conter a palavra de ativação
    "Estou aqui.",
    "À sua disposição.",
    "Pois não?",
    "Sim, senhor.",
    "Pronto.",
    "Estou ouvindo.",
    "Como posso ajudar?",
    "Às suas ordens.",
    "Pode falar.",
    "Estou à escuta.",
]


def api_key() -> str:
    from app.core.accounts import credential
    return credential("GEMINI_API_KEY")


@dataclass
class Settings:
    # Voz
    input_device: int | None = None
    output_device: int | None = None
    tts_engine: str = "edge"            # "edge" (neural, online) | "kokoro" (local, offline) | "sapi" (Windows, offline)
    tts_voice: str = "pt-BR-AntonioNeural"
    tts_rate: int = -4                  # % relativo à velocidade normal (-50..+50); negativo = mais calmo
    tts_pitch_hz: int = -6              # edge-tts: deslocamento do tom em Hz (negativo = mais grave)
    tts_bass_db: float = 2.5            # realce de graves (0 desliga); dá corpo à voz
    tts_pause_ms: int = 140             # pausa entre frases (a de vírgula/dois-pontos é proporcional)
    kokoro_voice: str = "pm_alex"       # voz local (pm_alex, pm_santa = pt-BR masculinas; bm_* = britânicas)
    kokoro_lang: str = "pt-br"          # idioma de fonemas do Kokoro ("pt-br", "en-gb", ...)
    kokoro_pitch_st: float = -1.0       # semitons (negativo = mais grave); o tempo é compensado
    kokoro_model_dir: str = ""          # vazio = ./models/kokoro ou %APPDATA%/APOLO/models/kokoro
    tts_cache: bool = True              # guarda frases curtas já sintetizadas (confirmações, erros) em disco
    stt_engine: str = "google"          # "google" | "gemini" | "whisper" (local, offline)
    whisper_model: str = "small"        # tiny | base | small | medium | large-v3-turbo ...
    whisper_device: str = "cpu"         # "cpu" | "cuda" | "auto"
    whisper_compute: str = "int8"       # int8 (CPU) | float16 (GPU)
    language: str = "pt-BR"
    # Fim da fala (VAD)
    vad_aggressiveness: int = 2         # 0..3 (webrtcvad)
    vad_min_rms: float = 0.003          # descarta energia quase inaudível; 0 desativa o filtro
    vad_end_silence_ms: int = 500       # silêncio que confirma o fim da fala
    vad_soft_silence_ms: int = 240      # começa a transcrever enquanto confirma o fim
    stt_speculative: bool = True        # começa a transcrever antes do fim confirmado (descarta se voltar a falar)
    # Ativação por voz ("Apolo"): VAD + faster-whisper LOCAL. O modelo é baixado na 1ª vez (precisa de internet nessa vez).
    wake_word_enabled: bool = WAKE_WORD_ENABLED
    wake_word: str = WAKE_WORD
    wake_model: str = WAKE_WORD_MODEL   # tiny (mais rápido) | base | small (mais preciso). Se == whisper_model e stt=whisper, é compartilhado
    wake_engine: str = "hybrid"        # detector contínuo + conferência em português | whisper
    wake_keyword_threshold: float = 0.15  # limiar acústico do detector contínuo
    wake_rearm_s: float = 0.35          # pausa curta após uma resposta, além da guarda de eco
    wake_low_latency: bool = WAKE_WORD_LOW_LATENCY
    wake_min_confidence: float = WAKE_WORD_MIN_CONFIDENCE
    wake_pre_buffer_ms: int = WAKE_WORD_PRE_BUFFER_MS
    wake_post_buffer_ms: int = WAKE_WORD_POST_BUFFER_MS
    wake_end_silence_ms: int = 300
    wake_soft_silence_ms: int = 140
    wake_prefix_s: float = 0.7         # verifica o começo da fala contínua; 0 desativa
    activation_feedback: str = "tone" # tone (sinal curto) | voice (frase) | silent
    wake_max_utterance_s: float = 8.0   # frases mais longas que isso são cortadas (poupa CPU com TV/conversa ao fundo)
    wake_post_speech_guard_s: float = 0.8   # TETO da guarda de eco após o APOLO falar (no modo baixa latência ela termina antes, ver abaixo)
    wake_guard_min_s: float = 0.2       # mínimo da guarda: termina aqui se o microfone já voltou ao nível de ruído ambiente
    wake_music_max_voiced_s: float = 3.0    # com música tocando, frases com mais voz que isso (letra cantada) nem vão ao STT
    activation_cooldown_s: float = WAKE_WORD_COOLDOWN_MS / 1000.0
    # Música (provedores: Spotify [Premium], YouTube [chave de API grátis] ou sua pasta local). Chaves ficam no .env.
    music_enabled: bool = MUSIC_ENABLED
    music_provider: str = MUSIC_PROVIDER
    music_local_dir: str = ""           # vazio = pasta Música do usuário
    music_volume_step: int = 10         # "aumente/diminua o volume" mexe tantos pontos percentuais
    music_default_volume: int = 70      # volume inicial do player local
    music_duck_pct: int = 25            # enquanto o APOLO ouve/fala, o player local baixa para este % do volume
    # Palmas
    clap_enabled: bool = True
    clap_sensitivity: float = 0.30      # pico mínimo (0..1): menor = mais sensível
    clap_min_gap_ms: int = 100
    clap_max_gap_ms: int = 1000
    clap_cooldown_s: float = 2.0
    # IA
    gemini_model: str = DEFAULT_MODEL
    gemini_temperature: float = 0.7
    gemini_max_tokens: int = 400
    gemini_thinking: str = "minimal"    # "minimal" | "low" | "off" (ignorado por modelos sem esse recurso)
    persona_humor: float = 0.28         # chance de uma ironia seca por resposta (0 = nunca)
    # Clima
    weather_city: str = "Salvador"
    weather_country: str = "BR"
    weather_unit: str = "celsius"       # "celsius" | "fahrenheit"
    # Aplicativo
    start_with_windows: bool = False
    start_minimized: bool = False
    start_maximized: bool = True
    # Controle interno de migração de padrões (não editar)
    config_version: int = 5

    @classmethod
    def load(cls) -> "Settings":
        s = cls(gemini_model=os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL)
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return s
        except Exception:
            log.exception("settings.json inválido; usando padrões")
            return s
        if int(data.get("config_version", 1) or 1) < 2:
            # Versão 1 gravou tts_rate=10 (rápido). Os novos padrões (voz calma e grave) valem para quem nunca os mudou.
            if data.get("tts_rate") == 10:
                data.pop("tts_rate")
        if int(data.get("config_version", 1) or 1) < 3:
            # Atualiza somente valores iguais aos padrões antigos; mantém ajustes personalizados.
            for key, old in {"wake_model": "base", "wake_end_silence_ms": 450,
                             "wake_soft_silence_ms": 200, "vad_end_silence_ms": 600,
                             "vad_soft_silence_ms": 320}.items():
                if data.get(key) == old:
                    data.pop(key)
        if int(data.get("config_version", 1) or 1) < 4:
            for key, old in {"wake_model": "tiny", "wake_prefix_s": 1.0}.items():
                if data.get(key) == old:
                    data.pop(key)
        if int(data.get("config_version", 1) or 1) < 5:
            if data.get("wake_min_confidence") == 0.60:
                data.pop("wake_min_confidence")
        data["config_version"] = 5
        s.update_from(data)
        return s

    def update_from(self, data: dict) -> None:
        for f in fields(self):
            if f.name in data:
                try:
                    current = getattr(self, f.name)
                    value = data[f.name]
                    if isinstance(current, bool):
                        value = bool(value)
                    elif isinstance(current, int):
                        value = None if value is None else int(value)
                    elif isinstance(current, float):
                        value = float(value)
                    setattr(self, f.name, value)
                except (TypeError, ValueError):
                    pass

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
