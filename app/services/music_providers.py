"""Provedores de música. Todos legais: APIs OFICIAIS ou arquivos que você já tem. Nada de extrair áudio, quebrar DRM,
raspar páginas ou burlar autenticação.

  spotify  API Web oficial (OAuth PKCE, sem segredo no PC). Toca no app/dispositivo Spotify Connect do usuário.
           Exige conta Premium (regra do Spotify: controle de reprodução e, desde fev/2026, o dono do app dev precisa de
           Premium) e um Client ID gratuito. Suporta tudo: buscar, tocar, pausar, continuar, próxima, anterior, volume.
  youtube  API Data v3 oficial (chave grátis; 100 buscas/dia na cota padrão) só para ACHAR o vídeo; a reprodução é no
           player oficial do YouTube, aberto no navegador (com anúncios, como qualquer pessoa assistiria). O controle
           usa as teclas de mídia do Windows (pausar/continuar/próxima/anterior/volume do sistema).
  local    arquivos mp3/flac/ogg/wav da sua pasta de músicas, tocados pelo próprio APOLO (music_player.LocalPlayer).
           Sem chave, sem internet.

Interface comum (MusicService só fala com isto):
  available() -> (bool, motivo)   play(intent) -> PlayResult   more_by_artist(artist, exclude) -> PlayResult
  pause() resume() next() previous() stop()   volume_set(pct) -> int|None   volume_step(delta) -> int|None
  now_playing() -> dict|None      duck(on)    external_control: bool"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import random
import secrets
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse

from app.services.music_intents import MusicIntent
from app.services.music_player import SUPPORTED_EXT, LocalPlayer, LocalTrack, MediaKeys
from app.utils.textutils import normalize

log = logging.getLogger("apolo.music")


# ------------------------------------------------------------------------------------------- tipos
@dataclass
class Track:
    title: str
    artist: str = ""
    uri: str = ""            # spotify:track:..., id do vídeo, caminho do arquivo
    album: str = ""
    provider: str = ""

    @property
    def label(self) -> str:
        return f"{self.title}, {self.artist}" if self.artist else self.title


@dataclass
class PlayResult:
    kind: str                                  # "track" | "artist" | "any"
    track: Track | None = None
    artist: str = ""
    queue: list[Track] = field(default_factory=list)      # o que vem depois (para "próxima"/estado)

    @property
    def label(self) -> str:
        return self.track.label if self.track else self.artist


class MusicError(Exception):
    """`spoken`: a frase curta que o APOLO fala (já em português)."""

    def __init__(self, kind: str, spoken: str):
        super().__init__(spoken)
        self.kind, self.spoken = kind, spoken


def sim(query: str, cand: str) -> float:
    """Similaridade 0..1 entre o que foi pedido e um candidato (sem acento/caixa)."""
    q, c = normalize(query), normalize(cand)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    r = SequenceMatcher(None, q, c).ratio()
    return max(r, 0.9) if set(q.split()) <= set(c.split()) else r


# ------------------------------------------------------------------------------------------- Spotify
class _NoDevice(Exception):
    pass


class SpotifyProvider:
    name = "spotify"
    external_control = True
    API = "https://api.spotify.com/v1"
    AUTH_URL = "https://accounts.spotify.com/authorize"
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    SCOPES = "user-modify-playback-state user-read-playback-state user-top-read"
    REDIRECT = "http://127.0.0.1:8888/callback"      # o Spotify exige o IP de loopback explícito (não "localhost")

    def __init__(self, client_id: str, token_file: Path, http=None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, wake_app: Callable[[], None] | None = None):
        self.client_id = (client_id or "").strip()
        self.token_file = Path(token_file)
        self._http = http
        self.clock, self.sleep = clock, sleep
        self._wake_app = wake_app or self._start_spotify_app
        self._tok: dict | None = None

    @property
    def http(self):
        if self._http is None:
            import requests
            self._http = requests.Session()
        return self._http

    def available(self) -> tuple[bool, str]:
        if not self.client_id:
            return False, "SPOTIFY_CLIENT_ID não definido em Minha conta"
        if not self.token_file.is_file():
            return False, "Spotify não conectado (rode: python scripts/music_setup.py spotify)"
        return True, ""

    # ---- OAuth (PKCE) ------------------------------------------------------------------------------
    def _load(self) -> dict:
        if self._tok is None:
            try:
                self._tok = json.loads(self.token_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise MusicError("auth", "O Spotify ainda não está conectado. Rode o setup de música no computador.")
        return self._tok

    def _save(self, tok: dict) -> None:
        self._tok = tok
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(tok), encoding="utf-8")

    def _store(self, data: dict, old_refresh: str = "") -> None:
        self._save({"access_token": data["access_token"], "refresh_token": data.get("refresh_token") or old_refresh,
                    "expires_at": self.clock() + int(data.get("expires_in", 3600))})

    def _refresh(self) -> None:
        tok = self._load()
        r = self.http.request("POST", self.TOKEN_URL, timeout=8, data={
            "grant_type": "refresh_token", "refresh_token": tok.get("refresh_token", ""), "client_id": self.client_id})
        if r.status_code != 200:
            raise MusicError("auth", "A conexão com o Spotify expirou. Rode o setup de música de novo.")
        self._store(r.json(), tok.get("refresh_token", ""))

    def _access_token(self) -> str:
        tok = self._load()
        if tok.get("expires_at", 0) - 60 < self.clock():
            self._refresh()
            tok = self._tok
        return tok["access_token"]

    def login(self, opener: Callable[[str], object] = webbrowser.open, timeout_s: float = 180.0) -> None:
        """Conecta a conta (uma vez): abre o navegador no login oficial do Spotify e recebe o código em 127.0.0.1:8888."""
        if not self.client_id:
            raise MusicError("auth", "Defina SPOTIFY_CLIENT_ID em Minha conta (veja o README).")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        got: dict = {}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):                                          # noqa: N802
                q = parse_qs(urlparse(self.path).query)
                got.update({k: v[0] for k, v in q.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<h3>APOLO conectado ao Spotify. Pode fechar esta aba.</h3>".encode())

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 8888), H)
        srv.timeout = 1.0
        url = self.AUTH_URL + "?" + urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": self.REDIRECT, "scope": self.SCOPES,
            "state": state, "code_challenge_method": "S256", "code_challenge": challenge})
        try:
            opener(url)
            t0 = time.time()
            while "code" not in got and "error" not in got and time.time() - t0 < timeout_s:
                srv.handle_request()
        finally:
            srv.server_close()
        if got.get("state") != state or "code" not in got:
            raise MusicError("auth", f"Login do Spotify não concluído ({got.get('error', 'sem resposta')}).")
        r = self.http.request("POST", self.TOKEN_URL, timeout=10, data={
            "grant_type": "authorization_code", "code": got["code"], "redirect_uri": self.REDIRECT,
            "client_id": self.client_id, "code_verifier": verifier})
        if r.status_code != 200:
            raise MusicError("auth", "O Spotify recusou o login.")
        self._store(r.json())

    # ---- chamadas ------------------------------------------------------------------------------------
    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None, retry: bool = True):
        r = self.http.request(method, self.API + path, timeout=8, params=params, json=body,
                              headers={"Authorization": f"Bearer {self._access_token()}"})
        sc = r.status_code
        if sc == 401 and retry:
            self._refresh()
            return self._call(method, path, params, body, retry=False)
        if sc in (200, 201, 202, 204):
            return r
        if sc == 429:
            raise MusicError("network", "O Spotify pediu para eu esperar um instante. Tente de novo em seguida.")
        try:
            err = r.json().get("error", {})
        except Exception:
            err = {}
        reason = err.get("reason", "") if isinstance(err, dict) else ""
        if sc == 404 and reason == "NO_ACTIVE_DEVICE":
            raise _NoDevice()
        if sc == 403 and reason == "PREMIUM_REQUIRED":
            raise MusicError("premium", "O Spotify só deixa eu controlar a reprodução em contas Premium.")
        if sc == 403:
            raise MusicError("forbidden", "O Spotify não permitiu essa ação agora.")
        raise MusicError("network", "Não consegui falar com o Spotify agora.")

    @staticmethod
    def _start_spotify_app() -> None:
        try:
            if sys.platform == "win32":
                os.startfile("spotify:")                                  # noqa: S606 (protocolo registrado pelo app)
            else:
                webbrowser.open("spotify:")
        except OSError:
            pass

    def _device(self) -> str:
        for attempt in range(2):
            devs = self._call("GET", "/me/player/devices").json().get("devices", [])
            if devs:
                pick = next((d for d in devs if d.get("is_active")), None) or \
                    next((d for d in devs if d.get("type") == "Computer"), None) or devs[0]
                return pick["id"]
            if attempt == 0:
                self._wake_app()
                for _ in range(8):                                        # dá até ~8 s para o app abrir e se registrar
                    self.sleep(1.0)
                    if self._call("GET", "/me/player/devices").json().get("devices"):
                        break
        raise MusicError("nodevice", "Abra o Spotify neste computador para eu poder tocar.")

    def _play_body(self, body: dict | None) -> None:
        try:
            self._call("PUT", "/me/player/play", body=body)
        except _NoDevice:
            self._call("PUT", "/me/player/play", params={"device_id": self._device()}, body=body)

    def _search(self, q: str, types: str, limit: int = 5) -> dict:
        return self._call("GET", "/search", params={"q": q, "type": types, "limit": limit, "market": "from_token"}).json()

    # ---- interface ---------------------------------------------------------------------------------
    def play(self, intent: MusicIntent) -> PlayResult:
        if intent.kind == "any" or not (intent.query or intent.track):
            return self._play_any()
        variants = []
        if intent.track and intent.artist:
            variants.append((f'track:"{intent.track}" artist:"{intent.artist}"', "track"))
        variants.append((intent.query, "track,artist"))
        for q, types in variants:
            data = self._search(q, types)
            pick = self._choose(intent, data)
            if pick:
                return self._start(pick, data)
        raise MusicError("notfound", "Não encontrei essa música no Spotify.")

    @staticmethod
    def _choose(intent: MusicIntent, data: dict):
        want_artist = intent.artist or intent.query
        want_track = intent.track or intent.query
        artists = [(sim(want_artist, a["name"]), a) for a in (data.get("artists") or {}).get("items", []) if a]
        tracks = []
        for t in (data.get("tracks") or {}).get("items", []):
            if not t:
                continue
            a_name = (t.get("artists") or [{}])[0].get("name", "")
            s = max(sim(want_track, t["name"]), 0.95 * sim(intent.query, f"{t['name']} {a_name}"))
            if intent.artist:
                s *= 0.5 + 0.5 * sim(intent.artist, a_name)
            tracks.append((s, t))
        best_a = max(artists, key=lambda x: x[0], default=(0.0, None))
        best_t = max(tracks, key=lambda x: x[0], default=(0.0, None))
        if intent.kind == "artist":
            return ("artist", best_a[1]) if best_a[0] >= 0.6 else None
        if intent.kind == "track":
            return ("track", best_t[1]) if best_t[0] >= 0.5 else None
        if best_a[1] is not None and best_a[0] >= 0.85 and best_a[0] >= best_t[0]:
            return "artist", best_a[1]
        if best_t[1] is not None and best_t[0] >= 0.5:
            return "track", best_t[1]
        return ("artist", best_a[1]) if best_a[1] is not None and best_a[0] >= 0.7 else None

    def _start(self, pick, data: dict) -> PlayResult:
        kind, obj = pick
        if kind == "artist":
            self._play_body({"context_uri": obj["uri"]})
            return PlayResult("artist", artist=obj["name"])
        first_artist = (obj.get("artists") or [{}])[0]
        siblings = [t for t in (data.get("tracks") or {}).get("items", [])
                    if t and t["uri"] != obj["uri"] and (t.get("artists") or [{}])[0].get("id") == first_artist.get("id")]
        self._play_body({"uris": [obj["uri"]] + [t["uri"] for t in siblings][:9]})
        return PlayResult("track", Track(obj["name"], first_artist.get("name", ""), obj["uri"], provider="spotify"),
                          queue=[Track(t["name"], first_artist.get("name", ""), t["uri"], provider="spotify") for t in siblings])

    def _play_any(self) -> PlayResult:
        items = self._call("GET", "/me/top/tracks", params={"limit": 20, "time_range": "short_term"}).json().get("items", [])
        if not items:
            raise MusicError("notfound", "Ainda não sei o que você gosta de ouvir. Diga o nome de uma música ou artista.")
        random.shuffle(items)
        self._play_body({"uris": [t["uri"] for t in items]})
        t0 = items[0]
        return PlayResult("any", Track(t0["name"], (t0.get("artists") or [{}])[0].get("name", ""), t0["uri"], provider="spotify"),
                          queue=[Track(t["name"], (t.get("artists") or [{}])[0].get("name", ""), t["uri"], provider="spotify")
                                 for t in items[1:]])

    def more_by_artist(self, artist: str, exclude: set[str]) -> PlayResult:
        items = self._search(f'artist:"{artist}"', "track", limit=10).get("tracks", {}).get("items", [])
        fresh = [t for t in items if t and normalize(t["name"]) not in exclude] or [t for t in items if t]
        if not fresh:
            raise MusicError("notfound", f"Não encontrei outra música de {artist}.")
        t = random.choice(fresh)
        self._play_body({"uris": [t["uri"]]})
        return PlayResult("track", Track(t["name"], (t.get("artists") or [{}])[0].get("name", artist), t["uri"], provider="spotify"))

    def pause(self) -> None:
        self._call("PUT", "/me/player/pause")

    def resume(self) -> None:
        self._play_body(None)

    def next(self) -> None:
        self._call("POST", "/me/player/next")

    def previous(self) -> None:
        self._call("POST", "/me/player/previous")

    def stop(self) -> None:
        self.pause()                                      # a API não tem "parar": pausar é o equivalente

    def volume_set(self, pct: int) -> int:
        pct = max(0, min(100, int(pct)))
        self._call("PUT", "/me/player/volume", params={"volume_percent": pct})
        return pct

    def volume_step(self, delta: int) -> int | None:
        st = self.now_playing()
        cur = st.get("volume") if st else None
        return self.volume_set((50 if cur is None else cur) + delta)

    def now_playing(self) -> dict | None:
        r = self._call("GET", "/me/player")
        if r.status_code == 204 or not r.text:
            return None
        d = r.json()
        item = d.get("item") or {}
        trk = Track(item["name"], (item.get("artists") or [{}])[0].get("name", ""), item.get("uri", ""),
                    provider="spotify") if item else None
        return {"is_playing": bool(d.get("is_playing")), "volume": (d.get("device") or {}).get("volume_percent"), "track": trk}

    def duck(self, on: bool) -> None:
        pass                                              # uma chamada de rede por ativação seria mais lenta que a própria ativação


# ------------------------------------------------------------------------------------------- YouTube
class YouTubeProvider:
    name = "youtube"
    external_control = True
    SEARCH = "https://www.googleapis.com/youtube/v3/search"

    def __init__(self, api_key: str, cache_file: Path | None = None, http=None, opener: Callable[[str], object] = webbrowser.open,
                 keys: MediaKeys | None = None):
        self.api_key = (api_key or "").strip()
        self.cache_file = cache_file
        self._http = http
        self.opener = opener
        self.keys = keys or MediaKeys()
        self._cache: dict[str, list[dict]] = {}
        self._seen: list[str] = []
        self._loaded = False

    @property
    def http(self):
        if self._http is None:
            import requests
            self._http = requests.Session()
        return self._http

    def available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "YOUTUBE_API_KEY não definido em Minha conta"
        if not self.keys.available:
            return False, "o controle por teclas de mídia só existe no Windows"
        return True, ""

    # ---- busca (com cache em disco: cada search.list custa 100 das 10.000 unidades diárias) ----------
    def _cache_load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.cache_file:
            try:
                self._cache = json.loads(Path(self.cache_file).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._cache = {}

    def _search(self, q: str) -> list[dict]:
        self._cache_load()
        key = normalize(q)
        if key in self._cache:
            return self._cache[key]
        r = self.http.request("GET", self.SEARCH, timeout=8, params={
            "part": "snippet", "type": "video", "videoCategoryId": "10", "maxResults": 5, "q": q, "key": self.api_key})
        if r.status_code == 403:
            raise MusicError("quota", "A cota diária da API do YouTube acabou. Tente amanhã ou use outro serviço de música.")
        if r.status_code != 200:
            raise MusicError("network", "Não consegui buscar no YouTube agora.")
        items = []
        for it in r.json().get("items", []):
            vid = (it.get("id") or {}).get("videoId")
            sn = it.get("snippet") or {}
            if vid:
                chan = (sn.get("channelTitle") or "").replace(" - Topic", "")
                chan = chan[:-4] if chan.lower().endswith("vevo") else chan
                items.append({"id": vid, "title": html.unescape(sn.get("title", "")), "artist": chan.strip()})
        self._cache[key] = items
        if self.cache_file and items:
            try:
                Path(self.cache_file).parent.mkdir(parents=True, exist_ok=True)
                Path(self.cache_file).write_text(json.dumps(dict(list(self._cache.items())[-200:]), ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return items

    def _open(self, vid: str) -> None:
        # list=RD<id> = "Mix" do próprio YouTube: o botão/tecla "próxima" funciona e a música seguinte toca sozinha
        self.opener(f"https://www.youtube.com/watch?v={vid}&list=RD{vid}")

    def play(self, intent: MusicIntent) -> PlayResult:
        q = intent.query or "músicas populares"
        if intent.kind == "any" or not intent.query:
            q = "top músicas brasil"
        elif intent.kind == "artist":
            q = f"{intent.query} official"
        items = self._search(q)
        if not items:
            raise MusicError("notfound", "Não encontrei isso no YouTube.")
        want = intent.query or ""
        best = max(items, key=lambda i: sim(want, f"{i['title']} {i['artist']}") + (0.1 if "official" in i["title"].lower() else 0.0))
        self._seen.append(best["id"])
        self._open(best["id"])
        kind = "artist" if intent.kind == "artist" else ("any" if not intent.query or intent.kind == "any" else "track")
        trk = Track(best["title"], best["artist"], best["id"], provider="youtube")
        return PlayResult(kind, trk, artist=best["artist"] if kind == "artist" else "",
                          queue=[Track(i["title"], i["artist"], i["id"], provider="youtube") for i in items if i["id"] != best["id"]])

    def more_by_artist(self, artist: str, exclude: set[str]) -> PlayResult:
        items = [i for i in self._search(f"{artist} músicas") if i["id"] not in self._seen] or self._search(f"{artist} músicas")
        if not items:
            raise MusicError("notfound", f"Não encontrei outra música de {artist}.")
        pick = random.choice(items)
        self._seen.append(pick["id"])
        self._open(pick["id"])
        return PlayResult("track", Track(pick["title"], pick["artist"] or artist, pick["id"], provider="youtube"))

    def _key(self, name: str, times: int = 1) -> None:
        if not self.keys.press(name, times):
            raise MusicError("unsupported", "Só consigo controlar o YouTube no Windows.")

    def pause(self) -> None:
        self._key("play_pause")

    def resume(self) -> None:
        self._key("play_pause")

    def next(self) -> None:
        self._key("next")

    def previous(self) -> None:
        self._key("previous")

    def stop(self) -> None:
        self._key("play_pause")                          # navegador: pausar é o mais próximo de parar

    def volume_set(self, pct: int) -> int | None:
        raise MusicError("unsupported", "Com o YouTube só consigo aumentar ou diminuir o volume, não escolher um valor.")

    def volume_step(self, delta: int) -> int | None:
        self._key("volume_up" if delta > 0 else "volume_down", max(1, abs(delta) // 2))   # cada tecla = 2% do volume do Windows
        return None

    def now_playing(self) -> dict | None:
        return None                                      # o YouTube no navegador não informa o estado

    def duck(self, on: bool) -> None:
        pass


# ------------------------------------------------------------------------------------------- Local
_PRE = ("apolo",)


class LocalProvider:
    name = "local"
    external_control = False
    MAX_FILES = 30000

    def __init__(self, directory: str | Path, player: LocalPlayer | None = None, tags: bool = True):
        self.dir = Path(directory) if directory else Path.home() / "Music"
        self.player = player
        self.tags = tags
        self.index: list[LocalTrack] = []
        self._norm: list[tuple[str, str]] = []
        self._scanned = False
        self._scanning = False
        self._lock = threading.Lock()

    # ---- índice --------------------------------------------------------------------------------------
    def scan(self) -> int:
        with self._lock:
            if self._scanning:
                return len(self.index)
            self._scanning = True
        tracks: list[LocalTrack] = []
        try:
            if self.dir.is_dir():
                for root, _, files in os.walk(self.dir):
                    for f in files:
                        if f.lower().endswith(SUPPORTED_EXT):
                            tracks.append(self._describe(Path(root) / f))
                            if len(tracks) >= self.MAX_FILES:
                                break
                    if len(tracks) >= self.MAX_FILES:
                        break
            self.index = tracks
            self._norm = [(normalize(t.title), normalize(t.artist)) for t in tracks]
            self._scanned = True
            log.info("[MUSIC] Biblioteca local: %d faixas em %s", len(tracks), self.dir)
        finally:
            self._scanning = False
        return len(self.index)

    def _describe(self, p: Path) -> LocalTrack:
        title, artist, album = "", "", ""
        if self.tags:
            try:
                import mutagen
                f = mutagen.File(p, easy=True)
                if f is not None:
                    title = (f.get("title") or [""])[0]
                    artist = (f.get("artist") or [""])[0]
                    album = (f.get("album") or [""])[0]
            except Exception:
                pass
        if not title:
            stem = p.stem.lstrip("0123456789 ._-") or p.stem
            if " - " in stem:
                a, t = stem.split(" - ", 1)
                artist, title = artist or a.strip(), t.strip()
            else:
                title = stem.strip()
        if not artist:
            try:
                parts = p.relative_to(self.dir).parts
                if len(parts) >= 3:
                    artist, album = parts[0], album or parts[1]
                elif len(parts) == 2:
                    artist = parts[0]
            except ValueError:
                pass
        return LocalTrack(p, title, artist, album)

    def available(self) -> tuple[bool, str]:
        if not self.dir.is_dir():
            return False, f"pasta de músicas não encontrada ({self.dir})"
        if not self._scanned:
            return False, "indexando a biblioteca de músicas"
        if not self.index:
            return False, f"nenhum mp3/flac/ogg/wav em {self.dir}"
        return True, ""

    # ---- busca ---------------------------------------------------------------------------------------
    def _by_artist(self, name: str) -> list[LocalTrack]:
        n = normalize(name)
        return [t for t, (_, a) in zip(self.index, self._norm) if a and (n in a or sim(n, a) >= 0.8)]

    def _rank(self, intent: MusicIntent) -> list[tuple[float, LocalTrack]]:
        q = normalize(intent.query)
        toks = [w for w in q.split() if len(w) >= 3] or q.split()
        out = []
        for t, (nt, na) in zip(self.index, self._norm):
            if toks and not any(w in nt or w in na for w in toks):          # pré-filtro barato antes do SequenceMatcher
                continue
            if intent.track and intent.artist:
                s = 0.7 * sim(intent.track, t.title) + 0.3 * sim(intent.artist, t.artist)
            else:
                s = max(sim(q, t.title), 0.95 * sim(q, t.artist), 0.95 * sim(q, f"{t.artist} {t.title}"))
            out.append((s, t))
        out.sort(key=lambda x: x[0], reverse=True)
        return out

    def play(self, intent: MusicIntent) -> PlayResult:
        if not self.index:
            raise MusicError("notfound", "Não achei músicas na sua pasta.")
        if intent.kind == "any" or not intent.query:
            queue = random.sample(self.index, min(len(self.index), 50))
            kind, artist = "any", ""
        elif intent.kind == "artist":
            queue = self._by_artist(intent.artist or intent.query)
            random.shuffle(queue)
            kind, artist = "artist", intent.artist or intent.query
            if not queue:
                raise MusicError("notfound", f"Não achei músicas de {artist} na sua pasta.")
        else:
            ranked = self._rank(intent)
            if not ranked or ranked[0][0] < 0.6:
                raise MusicError("notfound", "Não achei essa música na sua pasta.")
            best_s, best = ranked[0]
            if intent.kind == "auto" and sim(intent.query, best.artist) >= max(0.8, sim(intent.query, best.title)):
                queue = self._by_artist(best.artist)
                random.shuffle(queue)
                kind, artist = "artist", best.artist
            else:
                rest = [t for t in self._by_artist(best.artist) if t is not best] if best.artist else []
                random.shuffle(rest)
                queue, kind, artist = [best] + rest, "track", ""
        self._start(queue)
        first = queue[0]
        if kind == "artist":
            artist = first.artist or artist                  # nome REAL do artista (o pedido chega normalizado: sem acento/caixa)
        return PlayResult(kind, None if kind == "artist" else Track(first.title, first.artist, str(first.path), provider="local"),
                          artist=artist, queue=[Track(t.title, t.artist, str(t.path), provider="local") for t in queue[1:11]])

    def _start(self, queue: list[LocalTrack]) -> None:
        if self.player is None:
            raise MusicError("unsupported", "O player local não está disponível.")
        self.player.play_queue(queue)

    def more_by_artist(self, artist: str, exclude: set[str]) -> PlayResult:
        pool = [t for t in self._by_artist(artist) if normalize(t.title) not in exclude] or self._by_artist(artist)
        if not pool:
            raise MusicError("notfound", f"Não achei outra música de {artist} na sua pasta.")
        pick = random.choice(pool)
        self._start([pick] + [t for t in pool if t is not pick][:20])
        return PlayResult("track", Track(pick.title, pick.artist, str(pick.path), provider="local"))

    # ---- controles ------------------------------------------------------------------------------------
    def _p(self) -> LocalPlayer:
        if self.player is None:
            raise MusicError("unsupported", "O player local não está disponível.")
        return self.player

    def pause(self) -> None:
        self._p().pause()

    def resume(self) -> None:
        self._p().resume()

    def next(self) -> None:
        if not self._p().next():
            raise MusicError("end", "Essa era a última da fila.")

    def previous(self) -> None:
        if not self._p().previous():
            raise MusicError("end", "Não há música anterior.")

    def stop(self) -> None:
        self._p().stop()

    def volume_set(self, pct: int) -> int:
        return self._p().set_volume(pct)

    def volume_step(self, delta: int) -> int:
        return self._p().set_volume(self._p().volume + delta)

    def now_playing(self) -> dict | None:
        p = self.player
        if p is None or p.current is None:
            return None
        c = p.current
        return {"is_playing": p.is_playing, "volume": p.volume, "track": Track(c.title, c.artist, str(c.path), provider="local")}

    def duck(self, on: bool) -> None:
        if self.player is not None:
            self.player.duck(on)
