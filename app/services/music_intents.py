"""Português falado -> intenção de música (função pura, sem rede, sem estado além de "há música ativa?").

    "toque Believer"                    -> play  query="believer"                    kind=auto
    "toque música do The Weeknd"        -> play  query="the weeknd"  artist=...      kind=artist
    "toque Believer do Imagine Dragons" -> play  track="believer" artist="imagine dragons"
    "toque música" / "coloque música para mim" -> play kind=any
    "pause" / "continue" / "próxima música" / "música anterior" / "pare a música"
    "aumente o volume" / "diminua o volume" / "volume 40"
    "toque outra do mesmo artista"      -> same_artist

Comandos curtos e ambíguos ("pause", "continue", "pare", "próxima", "volume") só valem se houver música em andamento
(ou se a frase citar música/som/faixa); senão voltam None e a frase segue o fluxo normal (Gemini).
O texto vem normalizado (minúsculas, sem acento, sem pontuação); quem fala o título é o provedor, com o nome real."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.utils.textutils import normalize


@dataclass(frozen=True)
class MusicIntent:
    action: str                 # play | pause | resume | next | previous | stop | volume_up | volume_down | volume_set | same_artist
    query: str = ""             # o que foi pedido, inteiro ("believer do imagine dragons")
    track: str = ""             # parte "faixa", quando a frase separa faixa e artista
    artist: str = ""
    kind: str = "auto"          # auto (faixa OU artista) | track | artist | any (qualquer música)
    amount: int | None = None   # volume_set: 0..100


_STRONG = r"(?:toque|tocar|toca|tocas|reproduza|reproduzir|reproduz|play|quero ouvir|quero escutar|ouvir|escutar|escute)"
_WEAK = r"(?:coloque|colocar|coloca|ponha|poe|bota|bote)"           # ambíguos ("coloque o computador para dormir")
_NOUN = r"(?:musica|musicas|som|sons|cancao|cancoes|album|albuns|playlist|faixa)"
_MUSIC_WORD = re.compile(rf"\b{_NOUN}\b|\bplayer\b|\breproducao\b|\btocando\b")
_NOT_MUSIC = re.compile(r"\b(?:computador|pc|notebook|tela|luz|luzes|alarme|timer|temporizador|lembrete|janela|modo|tv|"
                        r"televisao|ar|ventilador|despertador|cafe|agua|lampada|silencio|mudo|fogo|ordem|lugar|"
                        r"nota|nome|lista|compras|cronometro|aviso)\b")
_GENERIC = re.compile(rf"^(?:uma |umas |alguma |algumas |a |as |um |algum )?(?:{_NOUN})(?: aleatoria| aleatorias| qualquer"
                      rf"| quaisquer| ai| ae| legal| boa| boas)?$")
_NOUN_OF = re.compile(rf"^(?:uma |umas |alguma |algumas |a |as |o |os |um )?{_NOUN}\s+(?:do|da|de|dos|das)\s+(.+)$")
_NOUN_NAME = re.compile(rf"^(?:uma |umas |a |as |o |os |um )?(?P<noun>{_NOUN})\s+(?P<name>.+)$")
_TRACK_BY = re.compile(r"^(?P<track>.+?)\s+(?:do|da|dos|das|by)\s+(?P<artist>.+)$")      # "de" fica de fora: está em muitos títulos
_TAIL = re.compile(r"(?:\s+(?:por favor|pra mim|para mim|agora|ai|ae|pra gente|para a gente|de novo))+$")
_HEAD = re.compile(r"^(?:apolo )?(?:por favor )?(?:voce )?(?:pode |poderia |consegue |podia )?(?:me )?")


def _clean(t: str) -> str:
    return _TAIL.sub("", _HEAD.sub("", normalize(t))).strip()


def parse(text: str, active: bool = False) -> MusicIntent | None:
    """`active`: há música tocando/pausada (habilita os comandos curtos sem a palavra "música")."""
    t = _clean(text)
    if not t:
        return None
    words = t.split()
    has_music = bool(_MUSIC_WORD.search(t))
    short = len(words) <= 7

    # ---- "outra do mesmo artista"
    if re.search(r"\b(?:outra|outro|mais uma|mais um|uma outra)\b.*\b(?:mesmo|mesma)\b.*\b(?:artista|cantor|cantora|banda|grupo)\b", t):
        return MusicIntent("same_artist")

    # ---- volume (precisa de música ativa ou citar música)
    if re.search(r"\bvolume\b|\bmais (?:alto|baixo|forte|fraco)\b", t) and short and (active or has_music):
        m = re.search(r"\bvolume\b.*?\b(\d{1,3})\b", t) or re.search(r"\b(\d{1,3})\s*(?:por cento|%)", t)
        if m:
            return MusicIntent("volume_set", amount=max(0, min(100, int(m.group(1)))))
        if re.search(r"\b(?:maximo|no maximo|total)\b", t):
            return MusicIntent("volume_set", amount=100)
        if re.search(r"\b(?:minimo|no minimo)\b", t):
            return MusicIntent("volume_set", amount=10)
        if re.search(r"\bmetade\b", t):
            return MusicIntent("volume_set", amount=50)
        if re.search(r"\b(?:aumente|aumenta|aumentar|suba|sobe|subir|eleve|mais alto|mais forte)\b", t):
            return MusicIntent("volume_up")
        if re.search(r"\b(?:diminua|diminui|diminuir|abaixe|abaixa|baixe|baixa|baixar|reduza|reduzir|mais baixo|mais fraco)\b", t):
            return MusicIntent("volume_down")
        return None

    # ---- controles curtos
    if short and (active or has_music):
        if re.search(r"\b(?:pare|parar|para|encerre|encerrar|desligue|desligar|termine|stop)\b", t) and (has_music or len(words) <= 2):
            return MusicIntent("stop")
        if re.search(r"\b(?:pause|pausa|pausar|pausando)\b", t):
            return MusicIntent("pause")
        if re.search(r"\banterior\b", t) or (re.search(r"\b(?:volte|voltar|volta)\b", t) and has_music and not re.search(r"\btoc", t)):
            return MusicIntent("previous")
        if re.search(r"\b(?:proxima|proximo|avance|avancar|pule|pular|pula|skip|next)\b", t) or \
                (re.search(r"\b(?:troque|trocar|troca|mude|mudar|muda)\b", t) and has_music) or \
                re.fullmatch(r"(?:toque |toca |tocar )?(?:outra|outro)(?: musica| faixa| som)?", t):
            return MusicIntent("next")
        if re.search(r"\b(?:continue|continua|continuar|retome|retomar|resume|despause|despausar|prossiga|volte a tocar|volta a tocar)\b", t):
            return MusicIntent("resume")

    # ---- tocar
    strong = re.match(rf"^{_STRONG}(?:\s+(?P<rest>.+))?$", t)
    weak = re.match(rf"^{_WEAK}\s+(?P<rest>.+)$", t)
    m = strong or weak
    if not m:
        return None
    rest = (m.group("rest") or "").strip()
    if not rest:                                                    # "toque" sozinho: retoma, ou qualquer música
        return MusicIntent("resume") if active else MusicIntent("play", kind="any")
    if weak and not strong:                                         # "coloque ...": só se parecer música
        if _NOT_MUSIC.search(rest) or (not _MUSIC_WORD.search(rest) and len(rest.split()) > 4):
            return None
    if _GENERIC.match(rest):
        return MusicIntent("play", kind="any")
    mo = _NOUN_OF.match(rest)
    if mo:
        artist = mo.group(1).strip()
        return MusicIntent("play", query=artist, artist=artist, kind="artist")
    mn = _NOUN_NAME.match(rest)
    if mn:                                                          # "a música believer", "a playlist treino"
        name = mn.group("name").strip()
        mb = _TRACK_BY.match(name)
        kind = "any" if mn.group("noun") == "playlist" else "track"
        if mb:
            return MusicIntent("play", query=name, track=mb.group("track"), artist=mb.group("artist"), kind="track")
        return MusicIntent("play", query=name, kind=kind)
    rest = re.sub(r"^(?:o|a|os|as)\s+(?=\S+\s+\S+|\S{4,})", "", rest)   # artigo solto ("toque o believer")
    mb = _TRACK_BY.match(rest)
    if mb:
        return MusicIntent("play", query=rest, track=mb.group("track").strip(), artist=mb.group("artist").strip(), kind="track")
    return MusicIntent("play", query=rest, kind="auto")
