from enum import Enum


class State(str, Enum):
    IDLE = "IDLE"
    ACTIVATED = "ACTIVATED"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"
    ERROR = "ERROR"


STATE_LABELS = {
    State.IDLE: "PRONTO PARA AJUDAR",
    State.ACTIVATED: "ATIVADO",
    State.LISTENING: "OUVINDO...",
    State.PROCESSING: "PROCESSANDO...",
    State.SPEAKING: "RESPONDENDO...",
    State.ERROR: "ERRO",
}

# Cores (hex) de cada estado — usadas pelo núcleo animado e pela barra de status.
STATE_COLORS = {
    State.IDLE: "#1fb6d4",
    State.ACTIVATED: "#e6fbff",
    State.LISTENING: "#2ef2d0",
    State.PROCESSING: "#8f86ff",
    State.SPEAKING: "#3ab8ff",
    State.ERROR: "#ff4d6d",
}

# Fases do assistente (nomes usados nos logs) e como cada uma mapeia nos estados acima:
#   STANDBY / LISTENING_FOR_WAKE_WORD  -> IDLE        (palmas + "Apolo" armados)
#   WAKE_WORD_DETECTED / ACTIVATION_RESPONSE -> ACTIVATED
#   LISTENING_FOR_COMMAND              -> LISTENING
#   PROCESSING / SPEAKING              -> PROCESSING / SPEAKING   ... e de volta a IDLE
PHASES = {
    State.IDLE: "STANDBY",
    State.ACTIVATED: "ACTIVATION_RESPONSE",
    State.LISTENING: "LISTENING_FOR_COMMAND",
    State.PROCESSING: "PROCESSING",
    State.SPEAKING: "SPEAKING",
    State.ERROR: "ERROR",
}
