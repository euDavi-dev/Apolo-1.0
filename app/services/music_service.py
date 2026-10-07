"""Controlador central de música: escolhe o provedor, guarda o estado e devolve respostas CURTAS para o APOLO falar.

    MusicService.parse(texto)    -> MusicIntent | None      (music_intents.parse, ciente de "há música ativa?")
    MusicService.execute(intent) -> frase falada            (bloqueia na rede/disco; chamar na thread da sessão)

Estado mantido (para "Apolo, pause" sem pesquisar de novo e "toque outra do mesmo artista"):
    current_track, current_artist, is_playing, volume, queue, provider

Nada de lógica de música no assistant.py: ele só chama parse()/execute()/duck()."""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.core.config import DATA_DIR, Settings
from app.core.accounts import credential
from app.services import music_intents
from app.services.music_intents import MusicIntent
from app.services.music_player import LocalPlayer
from app.services.music_providers import (LocalProvider, MusicError, PlayResult, SpotifyProvider, YouTubeProvider)
from app.utils.textutils import normalize

log = logging.getLogger("apolo.music")

# frases fixas (viram áudio em cache no boot: respondem sem esperar a síntese)
MUSIC_FIXED_PHRASES = ["Pausando.", "Continuando a reprodução.", "Próxima música.", "Música anterior.",
                       "Parando a música.", "Aumentando o volume.", "Diminuindo o volume.", "Colocando música.",
                       "Não há música tocando."]
NO_PROVIDER = ("Ainda não configurei um serviço de música. Veja no README como ligar o Spotify, o YouTube "
               "ou a sua pasta de músicas.")


@dataclass
class MusicState:
    current_track: str = ""
    current_artist: str = ""
    is_playing: bool = False
    volume: int | None = None
    queue: list[str] = field(default_factory=list)
    provider: str = ""

    @property
    def active(self) -> bool:
        return bool(self.current_track or self.current_artist)


class MusicService:
    ORDER = ("spotify", "youtube", "local")          # ordem do modo "auto": o 1º que estiver configurado

    def __init__(self, cfg: Settings, providers: dict | None = None, on_state: Callable[[str, str], None] | None = None):
        self.cfg = cfg
        self.on_state = on_state                      # (texto, nível) para a linha "Música" do HUD
        self.state = MusicState(volume=None)
        self._providers = providers
        self._active: str | None = None
        self._recent: list[str] = []
        self._lock = threading.RLock()
        self.local: LocalProvider | None = None

    # ---- provedores ----------------------------------------------------------------------------------
    @property
    def providers(self) -> dict:
        if self._providers is None:
            c = self.cfg
            player = LocalPlayer(output_device=c.output_device, volume=c.music_default_volume,
                                 duck_pct=c.music_duck_pct, on_change=self._on_player_change)
            self.local = LocalProvider(c.music_local_dir or Path.home() / "Music", player)
            self._providers = {
                "spotify": SpotifyProvider(credential("SPOTIFY_CLIENT_ID"), DATA_DIR / "spotify_token.json"),
                "youtube": YouTubeProvider(credential("YOUTUBE_API_KEY"), DATA_DIR / "youtube_cache.json"),
                "local": self.local,
            }
        return self._providers

    def warm(self) -> None:
        """Indexa a pasta local em segundo plano (só se for relevante) — o boot e o wake word não esperam por isso."""
        if not self.cfg.music_enabled or self.cfg.music_provider not in ("auto", "local"):
            return
        local = self.providers.get("local")
        if hasattr(local, "scan"):
            threading.Thread(target=local.scan, daemon=True, name="music-index").start()

    def _pick(self) -> object:
        want = (self.cfg.music_provider or "auto").lower()
        provs = self.providers
        if want != "auto":
            p = provs.get(want)
            if p is None:
                raise MusicError("noprovider", NO_PROVIDER)
            ok, why = p.available()
            if not ok:
                raise MusicError("noprovider", f"O serviço {want} ainda não está pronto: {why}.")
            return p
        reasons = []
        for name in self.ORDER:
            p = provs.get(name)
            if p is None:
                continue
            ok, why = p.available()
            if ok:
                return p
            reasons.append(why)
        if any("indexando" in r for r in reasons):
            raise MusicError("indexing", "Ainda estou indexando as suas músicas. Tente em instantes.")
        raise MusicError("noprovider", NO_PROVIDER)

    def _control(self):
        """Provedor que recebe pausa/continua/próxima...: o último usado; senão um serviço externo configurado."""
        if self._active and self._active in self.providers:
            return self.providers[self._active]
        for name in ("spotify", "youtube"):
            p = self.providers.get(name)
            if p is not None and p.available()[0]:
                return p
        raise MusicError("none", "Não há música tocando.")

    # ---- interface para o assistant -----------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.state.active

    @property
    def is_playing(self) -> bool:
        return self.state.is_playing

    def parse(self, text: str) -> MusicIntent | None:
        if not self.cfg.music_enabled:
            return None
        return music_intents.parse(text, active=self.state.active)

    def duck(self, on: bool) -> None:
        """Baixa o volume do player local enquanto o APOLO ouve/fala (não faz nada se não houver música local)."""
        if self._providers is None:
            return
        for p in self._providers.values():           # só o player local faz algo; os demais são no-op
            try:
                p.duck(on)
            except Exception:
                log.debug("duck falhou", exc_info=True)

    def execute(self, intent: MusicIntent) -> str:
        t0 = time.perf_counter()
        try:
            with self._lock:
                reply = self._execute(intent)
            log.info("[MUSIC] %s em %.0f ms -> %s", intent.action, (time.perf_counter() - t0) * 1000, reply)
            return reply
        except MusicError as e:
            log.info("[MUSIC] %s falhou (%s): %s", intent.action, e.kind, e.spoken)
            return e.spoken
        except Exception:
            log.exception("Falha no controle de música")
            return "Não consegui controlar a música agora."

    def shutdown(self) -> None:
        if self._providers and "local" in self._providers:
            try:
                self._providers["local"].stop()
            except Exception:
                pass

    # ---- ações ---------------------------------------------------------------------------------------
    def _execute(self, it: MusicIntent) -> str:
        a = it.action
        if a == "play":
            p = self._pick()
            res = p.play(it)
            self._started(res, p)
            if res.kind == "artist":
                return f"Colocando {res.artist}."
            return f"Reproduzindo {res.label}." if res.track else "Colocando música."
        if a == "same_artist":
            artist = self.state.current_artist
            if not artist:
                raise MusicError("noartist", "Não sei de qual artista é a música atual.")
            p = self._control()
            res = p.more_by_artist(artist, set(self._recent))
            self._started(res, p)
            return f"Reproduzindo {res.label}."
        p = self._control()
        if a == "pause":
            p.pause()
            self.state.is_playing = False
            self._emit()
            return "Pausando."
        if a == "resume":
            p.resume()
            self.state.is_playing = True
            self._emit()
            return "Continuando a reprodução."
        if a == "next":
            p.next()
            self.state.is_playing = True
            self._refresh_async(p)
            return "Próxima música."
        if a == "previous":
            p.previous()
            self.state.is_playing = True
            self._refresh_async(p)
            return "Música anterior."
        if a == "stop":
            p.stop()
            self.state = MusicState(volume=self.state.volume, provider=self.state.provider)
            self._emit()
            return "Parando a música."
        step = max(1, int(self.cfg.music_volume_step))
        if a in ("volume_up", "volume_down"):
            v = p.volume_step(step if a == "volume_up" else -step)
            self.state.volume = v if v is not None else self.state.volume
            return (f"Volume em {v} por cento." if v is not None
                    else ("Aumentando o volume." if a == "volume_up" else "Diminuindo o volume."))
        if a == "volume_set":
            v = p.volume_set(it.amount or 0)
            self.state.volume = v
            return f"Volume em {v} por cento."
        raise MusicError("unknown", "Não entendi esse comando de música.")

    def _started(self, res: PlayResult, prov) -> None:
        s = self.state
        s.provider, self._active = prov.name, prov.name
        s.current_track = res.track.title if res.track else ""
        s.current_artist = res.track.artist if res.track else res.artist
        s.is_playing = True
        s.queue = [t.label for t in res.queue]
        if res.track:
            self._recent = (self._recent + [normalize(res.track.title)])[-20:]
        self._emit()
        if res.kind != "track" or not res.track:
            self._refresh_async(prov, delay=1.5)           # artista/qualquer: descobre qual faixa começou

    def _refresh_async(self, prov, delay: float = 0.4) -> None:
        def work():
            time.sleep(delay)
            try:
                st = prov.now_playing()
            except Exception:
                return
            if st:
                self._apply_state(st)

        threading.Thread(target=work, daemon=True, name="music-refresh").start()

    def _apply_state(self, st: dict) -> None:
        s = self.state
        if st.get("track"):
            s.current_track, s.current_artist = st["track"].title, st["track"].artist
        s.is_playing = bool(st.get("is_playing"))
        if st.get("volume") is not None:
            s.volume = st["volume"]
        self._emit()

    def _on_player_change(self) -> None:
        """O player local mudou sozinho (fim da faixa, fila acabou): atualiza o estado."""
        local = (self._providers or {}).get("local")
        if local is None or self._active != "local":
            return
        st = local.now_playing()
        if st:
            self._apply_state(st)
        elif self.state.is_playing:
            self.state.is_playing = False
            self._emit()

    def _emit(self) -> None:
        if not self.on_state:
            return
        s = self.state
        if not s.active:
            self.on_state("OFF", "off")
        else:
            name = f"{s.current_track} — {s.current_artist}" if s.current_track else s.current_artist
            self.on_state(("▶ " if s.is_playing else "⏸ ") + name[:34], "ok")

    def snapshot(self) -> dict:
        s = self.state
        return {"current_track": s.current_track, "current_artist": s.current_artist, "is_playing": s.is_playing,
                "volume": s.volume, "queue": list(s.queue), "provider": s.provider}
