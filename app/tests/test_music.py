"""python -m app.tests.test_music — comandos de música, serviço, provedores e integração com o assistente.

IMPORTANTE (o que este teste É e NÃO É): usa dublês para a REDE (Spotify/YouTube), para a PLACA DE SOM (saída do player
local) e para as TECLAS DE MÍDIA. Prova a lógica, os pedidos HTTP montados, o estado e a integração. NÃO prova que o Spotify,
o YouTube ou a sua placa de som respondem: isso só se testa na sua máquina (veja o README, seção Música)."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import numpy as np

from app.tests import test_flow as tf
from app.tests.test_activation import (TTS, RecAudio, TONES, Hearing, load_clips, make, say, utterance, wait_idle)  # noqa: F401

import app.core.assistant as asm  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.services.music_intents import MusicIntent, parse  # noqa: E402
from app.services.music_player import LocalPlayer, LocalTrack, MediaKeys, PLAY_RATE  # noqa: E402
from app.services.music_providers import (LocalProvider, MusicError, PlayResult, SpotifyProvider, Track, YouTubeProvider)  # noqa: E402
from app.services.music_service import MusicService  # noqa: E402

check = tf.check


# ------------------------------------------------------------------------------------------------ 1. intenções
def t_intents() -> None:
    def P(text, active=False):
        return parse(text, active)

    def is_(text, action, active=False, **kw):
        r = P(text, active)
        ok = r is not None and r.action == action and all(getattr(r, k) == v for k, v in kw.items())
        check(f"intenção: {text!r}{' (música ativa)' if active else ''} -> {action}{kw or ''}", ok, repr(r))

    is_("Apolo, toque música.", "play", kind="any")
    is_("toque Imagine Dragons", "play", kind="auto", query="imagine dragons")
    is_("Toque Believer.", "play", kind="auto", query="believer")
    is_("toque Evidências", "play", query="evidencias")
    is_("coloque música para mim", "play", kind="any")
    is_("toque música do The Weeknd", "play", kind="artist", artist="the weeknd")
    is_("toque Believer do Imagine Dragons", "play", track="believer", artist="imagine dragons")
    is_("toque a música Believer", "play", kind="track", query="believer")
    is_("coloque The Weeknd", "play", query="the weeknd")
    is_("toque Dona de Mim", "play", query="dona de mim", track="")           # "de" no título não vira artista
    is_("pause", "pause", active=True)
    is_("pause a música", "pause")
    is_("continue", "resume", active=True)
    is_("próxima música", "next", active=True)
    is_("próxima", "next", active=True)
    is_("música anterior", "previous")
    is_("pare a música", "stop")
    is_("pare", "stop", active=True)
    is_("aumente o volume", "volume_up", active=True)
    is_("diminua o volume", "volume_down", active=True)
    is_("volume 40", "volume_set", active=True, amount=40)
    is_("toque outra do mesmo artista", "same_artist", active=True)
    for t, a in (("pause", False), ("continue", False), ("pare", False), ("aumente o volume", False), ("próxima", False),
                 ("que horas são", True), ("qual a previsão do tempo", True), ("abra o navegador", False),
                 ("desligue o computador", False), ("coloque o computador para dormir", False), ("obrigado", True)):
        check(f"intenção: {t!r}{' (música ativa)' if a else ''} NÃO é música", P(t, a) is None, repr(P(t, a)))


# ------------------------------------------------------------------------------------------------ 2. serviço
class FakeProv:
    name = "fake"
    external_control = False

    def __init__(self):
        self.calls, self.vol, self.cur = [], 50, None

    def available(self):
        return True, ""

    def play(self, intent):
        self.calls.append(("play", intent.query, intent.kind))
        if intent.kind == "artist":
            return PlayResult("artist", artist="The Weeknd")
        if intent.kind == "any" or not intent.query:
            self.cur = Track("Starboy", "The Weeknd")
            return PlayResult("any", self.cur)
        self.cur = Track("Believer", "Imagine Dragons")
        return PlayResult("track", self.cur, queue=[Track("Demons", "Imagine Dragons")])

    def more_by_artist(self, artist, exclude):
        self.calls.append(("more", artist, tuple(sorted(exclude))))
        return PlayResult("track", Track("Thunder", artist))

    def pause(self): self.calls.append(("pause",))
    def resume(self): self.calls.append(("resume",))
    def next(self): self.calls.append(("next",))
    def previous(self): self.calls.append(("previous",))
    def stop(self): self.calls.append(("stop",))

    def volume_set(self, pct):
        self.vol = pct
        self.calls.append(("vol", pct))
        return pct

    def volume_step(self, d):
        self.vol = max(0, min(100, self.vol + d))
        self.calls.append(("step", d))
        return self.vol

    def now_playing(self):
        return {"is_playing": True, "volume": self.vol, "track": self.cur}

    def duck(self, on): self.calls.append(("duck", on))


def svc(prov=None, **cfgkw) -> tuple[MusicService, FakeProv, list]:
    cfg = Settings()
    cfg.music_provider = "fake"
    for k, v in cfgkw.items():
        setattr(cfg, k, v)
    prov = prov or FakeProv()
    ui = []
    return MusicService(cfg, providers={"fake": prov}, on_state=lambda t, l: ui.append((t, l))), prov, ui


def t_service() -> None:
    s, p, ui = svc()
    run = lambda text: s.execute(s.parse(text))       # noqa: E731
    check("serviço: 'pause' sem nada tocando NÃO é tratado como música (vai ao fluxo normal)", s.parse("pause") is None)
    r = run("toque Believer")
    check("serviço: toque Believer -> 'Reproduzindo Believer, Imagine Dragons.'", r == "Reproduzindo Believer, Imagine Dragons.", r)
    check("serviço: estado após tocar (faixa, artista, tocando, fila)", s.snapshot() == {
        "current_track": "Believer", "current_artist": "Imagine Dragons", "is_playing": True, "volume": None,
        "queue": ["Demons, Imagine Dragons"], "provider": "fake"}, str(s.snapshot()))
    check("serviço: HUD recebeu '▶ Believer — Imagine Dragons'", ui and ui[-1][0].startswith("▶ Believer"), str(ui[-1:]))
    check("serviço: pause", run("pause") == "Pausando." and p.calls[-1] == ("pause",) and not s.state.is_playing)
    check("serviço: continue", run("continue") == "Continuando a reprodução." and s.state.is_playing and p.calls[-1] == ("resume",))
    check("serviço: próxima", run("próxima música") == "Próxima música." and p.calls[-1] == ("next",))
    check("serviço: anterior", run("música anterior") == "Música anterior." and p.calls[-1] == ("previous",))
    check("serviço: aumente o volume (+10)", run("aumente o volume") == "Volume em 60 por cento." and p.calls[-1] == ("step", 10))
    check("serviço: diminua o volume (-10)", run("diminua o volume") == "Volume em 50 por cento." and p.calls[-1] == ("step", -10))
    check("serviço: volume 30", run("volume 30") == "Volume em 30 por cento." and s.state.volume == 30)
    r = run("toque outra do mesmo artista")
    check("serviço: outra do mesmo artista usa o artista atual e exclui o que já tocou",
          r == "Reproduzindo Thunder, Imagine Dragons." and p.calls[-1][0] == "more" and p.calls[-1][1] == "Imagine Dragons"
          and "believer" in p.calls[-1][2], str(p.calls[-1]))
    check("serviço: pare a música zera o estado", run("pare a música") == "Parando a música." and not s.active and s.parse("pause") is None)
    check("serviço: música do artista -> 'Colocando The Weeknd.'", run("toque música do The Weeknd") == "Colocando The Weeknd.")
    check("serviço: 'toque música' genérico", run("toque música") == "Reproduzindo Starboy, The Weeknd.")
    s.duck(True)
    check("serviço: duck repassado ao provedor", p.calls[-1] == ("duck", True))
    s2, _, _ = svc(music_enabled=False)
    check("serviço: MUSIC_ENABLED=False desliga tudo", s2.parse("toque Believer") is None)
    s3 = MusicService(Settings(), providers={"spotify": SpotifyProvider("", Path("/nonexistent")),
                                             "youtube": YouTubeProvider(""), "local": LocalProvider("/nonexistent")})
    s3.cfg.music_provider = "auto"
    r = s3.execute(s3.parse("toque Believer"))
    check("serviço: nenhum provedor configurado -> orientação curta, sem fingir que toca", "Ainda não configurei" in r, r)
    check("serviço: ...e o estado continua parado", not s3.active)

    class Boom(FakeProv):
        def play(self, i): raise MusicError("notfound", "Não encontrei essa música.")
    s4, _, _ = svc(Boom())
    check("serviço: erro do provedor vira frase falada (sem 'tocando' falso)", s4.execute(s4.parse("toque xyz")) == "Não encontrei essa música." and not s4.active)

    s5 = MusicService(Settings(), providers={"spotify": SpotifyProvider("", Path("/x")), "youtube": FakeProv(),
                                             "local": LocalProvider("/nonexistent")})
    s5.cfg.music_provider = "auto"
    s5.providers["youtube"].name = "youtube"
    check("serviço: modo auto escolhe o 1º provedor disponível (spotify indisponível -> youtube)",
          s5.execute(s5.parse("toque Believer")) == "Reproduzindo Believer, Imagine Dragons." and s5.state.provider == "youtube")


# ------------------------------------------------------------------------------------------------ 3. Spotify
class Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._d = status, data
        self.text = "" if data is None else json.dumps(data)

    def json(self):
        return self._d


class FakeHttp:
    """Responde por (método, caminho); guarda todas as chamadas."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        path = url.split("api.spotify.com/v1")[-1] if "api.spotify.com" in url else url
        r = self.routes.get((method, path))
        if callable(r):
            r = r(kw)
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        return r if r is not None else Resp(204)


SEARCH_BELIEVER = {"tracks": {"items": [
    {"name": "Believer", "uri": "spotify:track:T1", "artists": [{"id": "A1", "name": "Imagine Dragons"}]},
    {"name": "Believer (Remix)", "uri": "spotify:track:T2", "artists": [{"id": "A9", "name": "Outro"}]},
    {"name": "Demons", "uri": "spotify:track:T3", "artists": [{"id": "A1", "name": "Imagine Dragons"}]}]},
    "artists": {"items": [{"name": "Believer Band", "uri": "spotify:artist:B", "id": "B"}]}}
SEARCH_ARTIST = {"tracks": {"items": [
    {"name": "Demons", "uri": "spotify:track:T3", "artists": [{"id": "A1", "name": "Imagine Dragons"}]}]},
    "artists": {"items": [{"name": "Imagine Dragons", "uri": "spotify:artist:A1", "id": "A1"}]}}


def spotify(tmp: Path, routes, now=1000.0, **kw) -> tuple[SpotifyProvider, FakeHttp]:
    tf_ = tmp / "tok.json"
    tf_.write_text(json.dumps({"access_token": "AT", "refresh_token": "RT", "expires_at": now + 3600}))
    http = FakeHttp(routes)
    return SpotifyProvider("CID", tf_, http=http, clock=lambda: now, sleep=lambda s: None, **kw), http


def t_spotify() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p, h = spotify(tmp, {("GET", "/search"): Resp(200, SEARCH_BELIEVER), ("PUT", "/me/player/play"): Resp(204)})
        r = p.play(MusicIntent("play", query="believer", kind="auto"))
        play = [c for c in h.calls if c[1].endswith("/me/player/play")][0]
        check("spotify: 'Believer' -> busca com Bearer e toca a faixa certa", r.kind == "track" and r.track.title == "Believer"
              and play[2]["json"]["uris"][0] == "spotify:track:T1" and play[2]["headers"]["Authorization"] == "Bearer AT",
              str(play[2].get("json")))
        check("spotify: a fila inclui outra faixa do MESMO artista, não a de outro", play[2]["json"]["uris"] == ["spotify:track:T1", "spotify:track:T3"])
        check("spotify: busca usa market=from_token e type=track,artist", h.calls[0][2]["params"]["type"] == "track,artist"
              and h.calls[0][2]["params"]["market"] == "from_token")

        p, h = spotify(tmp, {("GET", "/search"): Resp(200, SEARCH_ARTIST), ("PUT", "/me/player/play"): Resp(204)})
        r = p.play(MusicIntent("play", query="imagine dragons", kind="auto"))
        play = [c for c in h.calls if c[1].endswith("/me/player/play")][0]
        check("spotify: nome de artista -> toca o artista (context_uri)", r.kind == "artist" and play[2]["json"] == {"context_uri": "spotify:artist:A1"})

        p, h = spotify(tmp, {("GET", "/search"): Resp(200, SEARCH_BELIEVER), ("PUT", "/me/player/play"): Resp(204)})
        p.play(MusicIntent("play", query="believer do imagine dragons", track="believer", artist="imagine dragons", kind="track"))
        check("spotify: faixa + artista usa filtro estruturado track:/artist:", 'track:"believer" artist:"imagine dragons"' in h.calls[0][2]["params"]["q"])

        nodev = {"error": {"status": 404, "reason": "NO_ACTIVE_DEVICE"}}
        p, h = spotify(tmp, {("GET", "/search"): Resp(200, SEARCH_BELIEVER),
                             ("PUT", "/me/player/play"): [Resp(404, nodev), Resp(204)],
                             ("GET", "/me/player/devices"): Resp(200, {"devices": [{"id": "DEV1", "type": "Computer", "is_active": False}]})})
        p.play(MusicIntent("play", query="believer", kind="auto"))
        plays = [c for c in h.calls if c[1].endswith("/me/player/play")]
        check("spotify: sem dispositivo ativo -> escolhe o Computer e repete com device_id",
              len(plays) == 2 and plays[1][2]["params"] == {"device_id": "DEV1"})

        woke = []
        p, h = spotify(tmp, {("GET", "/search"): Resp(200, SEARCH_BELIEVER), ("PUT", "/me/player/play"): Resp(404, nodev),
                             ("GET", "/me/player/devices"): Resp(200, {"devices": []})}, wake_app=lambda: woke.append(1))
        try:
            p.play(MusicIntent("play", query="believer", kind="auto"))
            err = None
        except MusicError as e:
            err = e
        check("spotify: nenhum dispositivo -> tenta abrir o app e depois pede para abrir o Spotify (não finge tocar)",
              err is not None and err.kind == "nodevice" and woke == [1], str(err))

        p, h = spotify(tmp, {("GET", "/search"): Resp(200, SEARCH_BELIEVER),
                             ("PUT", "/me/player/play"): Resp(403, {"error": {"status": 403, "reason": "PREMIUM_REQUIRED"}})})
        try:
            p.play(MusicIntent("play", query="believer", kind="auto"))
            err = None
        except MusicError as e:
            err = e
        check("spotify: conta sem Premium -> mensagem clara", err is not None and err.kind == "premium" and "Premium" in err.spoken)

        p, h = spotify(tmp, {("GET", "/search"): Resp(200, {"tracks": {"items": [
            {"name": "Outra Coisa", "uri": "spotify:track:Z", "artists": [{"id": "Z", "name": "Fulano"}]}]}, "artists": {"items": []}})})
        try:
            p.play(MusicIntent("play", query="believer", kind="auto"))
            err = None
        except MusicError as e:
            err = e
        check("spotify: resultado que não bate com o pedido -> 'não encontrei' (não toca qualquer coisa)",
              err is not None and err.kind == "notfound")

        # controles
        p, h = spotify(tmp, {("PUT", "/me/player/pause"): Resp(204), ("POST", "/me/player/next"): Resp(204),
                             ("POST", "/me/player/previous"): Resp(204), ("PUT", "/me/player/volume"): Resp(204),
                             ("PUT", "/me/player/play"): Resp(204),
                             ("GET", "/me/player"): Resp(200, {"is_playing": True, "device": {"volume_percent": 40},
                                                                "item": {"name": "Believer", "uri": "u", "artists": [{"name": "Imagine Dragons"}]}})})
        p.pause(); p.resume(); p.next(); p.previous()
        v = p.volume_step(10)
        paths = [(c[0], c[1].split("/v1")[-1]) for c in h.calls]
        check("spotify: pause/continue/próxima/anterior chamam os endpoints oficiais",
              paths[:4] == [("PUT", "/me/player/pause"), ("PUT", "/me/player/play"), ("POST", "/me/player/next"),
                            ("POST", "/me/player/previous")], str(paths))
        check("spotify: volume +10 lê o volume atual (40) e envia 50", v == 50 and h.calls[-1][2]["params"] == {"volume_percent": 50})
        st = p.now_playing()
        check("spotify: now_playing devolve faixa/artista/volume", st["track"].title == "Believer" and st["volume"] == 40 and st["is_playing"])

        # token: expirado -> refresh; 401 -> refresh e repete
        tok = tmp / "tok2.json"
        tok.write_text(json.dumps({"access_token": "OLD", "refresh_token": "RT", "expires_at": 10}))
        http = FakeHttp({("POST", "https://accounts.spotify.com/api/token"): Resp(200, {"access_token": "NEW", "expires_in": 3600}),
                         ("PUT", "/me/player/pause"): Resp(204)})
        p = SpotifyProvider("CID", tok, http=http, clock=lambda: 1000.0, sleep=lambda s: None)
        p.pause()
        check("spotify: token expirado é renovado com o refresh_token (sem segredo, PKCE)",
              http.calls[0][2]["data"]["grant_type"] == "refresh_token" and http.calls[0][2]["data"]["client_id"] == "CID"
              and http.calls[1][2]["headers"]["Authorization"] == "Bearer NEW")
        check("spotify: refresh_token antigo é mantido quando o servidor não manda outro", json.loads(tok.read_text())["refresh_token"] == "RT")

        tok.write_text(json.dumps({"access_token": "A", "refresh_token": "RT", "expires_at": 99999}))
        http = FakeHttp({("POST", "https://accounts.spotify.com/api/token"): Resp(200, {"access_token": "B", "expires_in": 3600}),
                         ("PUT", "/me/player/pause"): [Resp(401, {"error": {}}), Resp(204)]})
        p = SpotifyProvider("CID", tok, http=http, clock=lambda: 1000.0, sleep=lambda s: None)
        p.pause()
        check("spotify: 401 -> renova e repete a chamada", [c[0] for c in http.calls] == ["PUT", "POST", "PUT"])

        p = SpotifyProvider("CID", tmp / "nao_existe.json", http=FakeHttp({}))
        check("spotify: sem login feito, available() explica o que fazer", not p.available()[0] and "music_setup" in p.available()[1])

        # login PKCE (servidor local real em 127.0.0.1:8888; navegador e Spotify simulados)
        tokf = tmp / "login.json"
        http = FakeHttp({("POST", "https://accounts.spotify.com/api/token"): Resp(200, {"access_token": "LA", "refresh_token": "LR", "expires_in": 3600})})
        p = SpotifyProvider("CID", tokf, http=http, clock=lambda: 1000.0)

        def fake_browser(url):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(url).query)
            assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == ["http://127.0.0.1:8888/callback"]
            assert "user-modify-playback-state" in q["scope"][0]
            threading.Thread(target=lambda: (time.sleep(0.2), urlopen(f"http://127.0.0.1:8888/callback?code=XYZ&state={q['state'][0]}").read()),
                             daemon=True).start()
        p.login(opener=fake_browser, timeout_s=10)
        ex = http.calls[0][2]["data"]
        saved = json.loads(tokf.read_text())
        check("spotify: login PKCE troca o código com code_verifier (sem client_secret) e guarda os tokens",
              ex["grant_type"] == "authorization_code" and ex["code"] == "XYZ" and len(ex["code_verifier"]) >= 43
              and "client_secret" not in ex and saved["access_token"] == "LA" and saved["refresh_token"] == "LR")


# ------------------------------------------------------------------------------------------------ 4. YouTube
YT = {"items": [
    {"id": {"videoId": "V1"}, "snippet": {"title": "Imagine Dragons - Believer (Official Music Video)", "channelTitle": "ImagineDragonsVEVO"}},
    {"id": {"videoId": "V2"}, "snippet": {"title": "Believer cover guitar", "channelTitle": "Someone"}}]}


def t_youtube() -> None:
    with tempfile.TemporaryDirectory() as d:
        sent, opened = [], []
        keys = MediaKeys(sender=sent.append)
        http = FakeHttp({("GET", "https://www.googleapis.com/youtube/v3/search"): Resp(200, YT)})
        p = YouTubeProvider("KEY", Path(d) / "c.json", http=http, opener=opened.append, keys=keys)
        r = p.play(MusicIntent("play", query="believer", kind="auto"))
        check("youtube: busca na API oficial com a chave e categoria Música", http.calls[0][2]["params"]["key"] == "KEY"
              and http.calls[0][2]["params"]["videoCategoryId"] == "10")
        check("youtube: abre o vídeo certo no player oficial, em modo Mix (próxima funciona)",
              opened == ["https://www.youtube.com/watch?v=V1&list=RDV1"] and r.track.title.startswith("Imagine Dragons - Believer"))
        check("youtube: artista deduzido do canal (sem VEVO)", r.track.artist == "ImagineDragons")
        p.play(MusicIntent("play", query="believer", kind="auto"))
        check("youtube: a mesma busca vem do cache (poupa a cota de 100 unidades)", len(http.calls) == 1)
        p2 = YouTubeProvider("KEY", Path(d) / "c.json", http=FakeHttp({}), opener=opened.append, keys=keys)
        p2.play(MusicIntent("play", query="believer", kind="auto"))
        check("youtube: cache sobrevive entre execuções (arquivo)", len(p2.http.calls) == 0)
        p.pause(); p.next(); p.previous(); p.volume_step(10)
        check("youtube: pausa/próxima/anterior/volume viram teclas de mídia do Windows",
              sent == [0xB3, 0xB0, 0xB1] + [0xAF] * 5, str([hex(x) for x in sent]))
        try:
            p.volume_set(30)
            err = None
        except MusicError as e:
            err = e
        check("youtube: volume absoluto não é suportado e diz isso", err is not None and err.kind == "unsupported")
        q = YouTubeProvider("KEY", http=FakeHttp({("GET", "https://www.googleapis.com/youtube/v3/search"): Resp(403, {})}),
                            opener=opened.append, keys=keys)
        try:
            q.play(MusicIntent("play", query="outra coisa", kind="auto"))
            err = None
        except MusicError as e:
            err = e
        check("youtube: cota esgotada -> mensagem clara", err is not None and err.kind == "quota")
        check("youtube: sem chave, available() explica", not YouTubeProvider("", keys=keys).available()[0])


# ------------------------------------------------------------------------------------------------ 5. player local
class FakeStream:
    def __init__(self, cb):
        self.cb, self.started = cb, False

    def start(self): self.started = True
    def stop(self): self.started = False
    def close(self): pass

    def pull(self, frames=1024):
        out = np.zeros((frames, 2), dtype=np.int16)
        self.cb(out, frames, None, None)
        return out


def make_player(n_frames=PLAY_RATE * 2, **kw):
    streams = []

    def factory(cb, dev):
        streams.append(FakeStream(cb))
        return streams[-1]

    pl = LocalPlayer(decoder=lambda path: np.full((n_frames, 2), 10000, dtype=np.int16), stream_factory=factory, **kw)
    return pl, streams


def wait_for(cond, t=3.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < t:
        time.sleep(0.01)
    return cond()


def t_player() -> None:
    pl, streams = make_player(volume=70, duck_pct=25)
    tracks = [LocalTrack(Path(f"/m/{i}.mp3"), t, "Banda") for i, t in enumerate(("Um", "Dois", "Três"))]
    pl.play_queue(tracks)
    check("player: toca de verdade (decodifica, abre a saída e anda)", wait_for(lambda: pl._samples is not None and streams)
          and streams[0].started and pl.current.title == "Um" and pl.is_playing)
    streams[0].pull(); out = streams[0].pull()
    check("player: volume 70% -> ganho (0,7)² = 0,49 sobre a amostra 10000", abs(int(out[100, 0]) - 4900) <= 2, str(out[100, 0]))
    pl.set_volume(50); streams[0].pull(); out = streams[0].pull()
    check("player: volume 50% -> 2500", abs(int(out[100, 0]) - 2500) <= 2, str(out[100, 0]))
    pl.duck(True); streams[0].pull(); out = streams[0].pull()
    check("player: duck (25% do volume) baixa de verdade e pode voltar", int(out[100, 0]) < 300)
    pl.duck(False); streams[0].pull(); out = streams[0].pull()
    check("player: ao fim do duck o volume volta", abs(int(out[100, 0]) - 2500) <= 2)
    pl.pause(); streams[0].pull(); pos = pl._pos; out = streams[0].pull(); out2 = streams[0].pull()
    check("player: pausa silencia e NÃO avança a posição", int(np.abs(out2).max()) == 0 and pl._pos == pos and not pl.is_playing)
    pl.resume(); streams[0].pull(); out = streams[0].pull()
    check("player: continua do mesmo ponto", pl.is_playing and int(out[100, 0]) > 0 and pl._pos > pos)
    check("player: próxima", pl.next() and wait_for(lambda: pl.current.title == "Dois" and pl._samples is not None and pl._pos == 0))
    check("player: anterior (início da faixa) volta uma", pl.previous() and wait_for(lambda: pl.current.title == "Um"))
    pl.stop()
    check("player: parar fecha a saída e limpa", not pl.is_playing and pl._samples is None and not streams[-1].started)

    pl, streams = make_player(n_frames=3000, volume=70)
    pl.play_queue(tracks)
    wait_for(lambda: pl._samples is not None and streams)
    for _ in range(3):                                   # 3 x 1024 > 3000 quadros: acaba só a 1ª faixa
        streams[0].pull(1024)
    check("player: ao fim da faixa passa sozinho para a próxima (fila)", wait_for(lambda: pl.current.title == "Dois"), pl.current.title)
    for _ in range(40):
        if streams[-1].started:
            streams[-1].pull(1024)
        time.sleep(0.02)
    check("player: no fim da fila para sozinho", wait_for(lambda: not pl.is_playing))
    bad = LocalPlayer(decoder=lambda p: (_ for _ in ()).throw(RuntimeError("arquivo ruim")), stream_factory=lambda cb, d: FakeStream(cb))
    bad.play_queue(tracks)
    check("player: arquivo que não decodifica é pulado sem travar", wait_for(lambda: not bad.is_playing and bad.index >= 2))


def t_decoder() -> None:
    """Decodificação REAL (sem dublê) pelo caminho de WAV da biblioteca padrão; mp3/flac exigem o miniaudio (no seu PC)."""
    import wave
    from app.services.music_player import default_decoder
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "tom.wav"
        x = (np.sin(2 * np.pi * 440 * np.arange(22050) / 22050) * 12000).astype(np.int16)
        with wave.open(str(f), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050); w.writeframes(x.tobytes())
        try:
            import miniaudio  # noqa: F401
            has = True
        except ImportError:
            has = False
        y = default_decoder(f)
        check("decodificador: WAV mono 22,05 kHz vira estéreo int16 a 44,1 kHz (~1 s)", y.dtype == np.int16 and y.shape[1] == 2
              and abs(len(y) - PLAY_RATE) <= 2 and int(np.abs(y).max()) > 11000, f"{y.shape}, miniaudio={'sim' if has else 'não'}")
        if not has:
            try:
                default_decoder(Path(d) / "x.mp3")
                err = ""
            except RuntimeError as e:
                err = str(e)
            check("decodificador: MP3 sem miniaudio explica o que instalar (não falha em silêncio)", "miniaudio" in err)


def t_player_race() -> None:
    """pausa -> continua ENQUANTO a faixa ainda decodifica (decodificador lento): tem de acabar tocando."""
    gate = threading.Event()
    pl = LocalPlayer(decoder=lambda p: (gate.wait(3), np.full((5000, 2), 9000, dtype=np.int16))[1],
                     stream_factory=lambda cb, d: FakeStream(cb))
    pl.play_queue([LocalTrack(Path("/m/a.mp3"), "A", "X")])
    pl.pause(); pl.resume()
    gate.set()
    check("player: continue durante a decodificação mantém a faixa tocando", wait_for(lambda: pl._samples is not None) and pl.is_playing)
    gate.clear()
    pl2 = LocalPlayer(decoder=lambda p: (gate.wait(3), np.full((5000, 2), 9000, dtype=np.int16))[1],
                      stream_factory=lambda cb, d: FakeStream(cb))
    pl2.play_queue([LocalTrack(Path("/m/a.mp3"), "A", "X")])
    pl2.pause()
    gate.set()
    wait_for(lambda: pl2._samples is not None)
    check("player: pausar durante a decodificação mantém a faixa PAUSADA quando ela fica pronta", not pl2.is_playing)


def t_media_keys() -> None:
    sent = []
    k = MediaKeys(sender=sent.append)
    k.press("play_pause"); k.press("volume_down", 3)
    check("teclas de mídia: códigos VK corretos (0xB3 play/pause, 0xAE volume-)", sent == [0xB3, 0xAE, 0xAE, 0xAE])
    check("teclas de mídia: fora do Windows, sem sender, não finge", sys.platform == "win32" or MediaKeys().press("next") is False)


# ------------------------------------------------------------------------------------------------ 6. provedor local
def t_local() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for rel in ("Imagine Dragons/Evolve/01 - Believer.mp3", "Imagine Dragons/Night Visions/02 - Demons.mp3",
                    "The Weeknd/After Hours/Starboy.flac", "Chitãozinho e Xororó - Evidências.mp3", "leia-me.txt", "capa.jpg"):
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
        pl, streams = make_player(volume=70)
        prov = LocalProvider(root, pl, tags=False)
        check("local: antes de indexar, available() diz que está indexando", prov.available() == (False, "indexando a biblioteca de músicas"))
        n = prov.scan()
        check("local: indexa só áudio suportado (4 faixas; ignora .txt/.jpg)", n == 4 and prov.available()[0], str(n))
        titles = {(t.title, t.artist) for t in prov.index}
        check("local: título/artista vindos do nome do arquivo e da pasta", ("Believer", "Imagine Dragons") in titles
              and ("Evidências", "Chitãozinho e Xororó") in titles and ("Starboy", "The Weeknd") in titles, str(sorted(titles)))
        s = MusicService(Settings(), providers={"local": prov})
        s.cfg.music_provider = "local"
        run = lambda text: s.execute(s.parse(text))        # noqa: E731
        r = run("toque Believer")
        check("local: 'toque Believer' toca a faixa certa de verdade", r == "Reproduzindo Believer, Imagine Dragons."
              and wait_for(lambda: pl.current and pl.current.title == "Believer" and pl.is_playing), r)
        check("local: a fila continua com outra do mesmo artista", [t.title for t in pl.queue] == ["Believer", "Demons"])
        wait_for(lambda: pl._samples is not None)
        check("local: pause de verdade", run("pause") == "Pausando." and not pl.is_playing)
        check("local: continue de verdade", run("continue") == "Continuando a reprodução." and pl.is_playing)
        check("local: próxima de verdade", run("próxima música") == "Próxima música." and wait_for(lambda: pl.current.title == "Demons"))
        check("local: anterior de verdade", run("música anterior") == "Música anterior." and wait_for(lambda: pl.current.title == "Believer"))
        check("local: volume +10 -> 80", run("aumente o volume") == "Volume em 80 por cento." and pl.volume == 80)
        check("local: volume -10 -> 70", run("diminua o volume") == "Volume em 70 por cento." and pl.volume == 70)
        check("local: volume 35", run("volume 35") == "Volume em 35 por cento." and pl.volume == 35)
        r = run("toque outra do mesmo artista")
        check("local: outra do mesmo artista toca outra faixa dele", r == "Reproduzindo Demons, Imagine Dragons." and
              wait_for(lambda: pl.current.title == "Demons"), r)
        r = run("toque música do The Weeknd")
        check("local: música do The Weeknd -> 'Colocando The Weeknd.'", r == "Colocando The Weeknd."
              and wait_for(lambda: pl.current and pl.current.artist == "The Weeknd"), r)
        r = run("toque Evidências")
        check("local: acentos não atrapalham ('Evidências' pedido sem acento pelo STT)", r.startswith("Reproduzindo Evidências"), r)
        r = run("toque Musica Que Nao Existe Mesmo")
        check("local: faixa inexistente -> 'Não achei', sem tocar", r == "Não achei essa música na sua pasta.")
        check("local: parar", run("pare a música") == "Parando a música." and not pl.is_playing and not s.active)
        r = run("toque música")
        check("local: 'toque música' toca algo da biblioteca", r.startswith("Reproduzindo") and wait_for(lambda: pl.is_playing), r)
        empty = LocalProvider(root / "vazia", pl)
        check("local: pasta inexistente -> indisponível com motivo", not empty.available()[0] and "não encontrada" in empty.available()[1])


# ------------------------------------------------------------------------------------------------ 7. assistente
def t_assistant() -> None:
    TONES.clear()
    a, ear, calls, listens = make(heard="pause", text="Apolo, toque Believer")
    prov = FakeProv()
    a.cfg.music_provider = "fake"
    a.music = MusicService(a.cfg, providers={"fake": prov}, on_state=lambda t, l: None)
    a.wake.noisy = lambda: a.music.is_playing
    with tempfile.TemporaryDirectory() as d:
        load_clips(a, Path(d))
        a._resume_listening()
        say(a, utterance(1.4))
        ok = wait_idle(a)
        check("assistente: 'Apolo, toque Believer' ativa e executa direto", calls == [("voice", "toque Believer")] and ok, str(calls))
        check("assistente: SEM 'Estou aqui' e SEM pedir para repetir", a.tts.played == [] and TONES == [] and len(listens) == 0)
        check("assistente: o provedor recebeu a busca e a música está ativa", ("play", "believer", "auto") in prov.calls and a.music.state.is_playing)
        check("assistente: resposta curta falada", a.tts.spoken == ["Reproduzindo Believer, Imagine Dragons."], str(a.tts.spoken))
        check("assistente: música local foi 'abaixada' (duck) durante a sessão e restaurada no fim",
              prov.calls.index(("duck", True)) < prov.calls.index(("play", "believer", "auto")) < prov.calls.index(("duck", False)))
        check("assistente: com música tocando o detector entra em modo música", a.wake.noisy() is True)

        # agora "Apolo" sozinho + "pause" (sem nova busca)
        a.wake.transcribe.text = "Apolo"
        a.tts.spoken.clear()
        a._last_activation -= 5
        a.wake._last_fire = -1e9
        say(a, utterance())
        wait_idle(a)
        check("assistente: 'Apolo' -> 'pause' pausa SEM pesquisar de novo", prov.calls.count(("play", "believer", "auto")) == 1
              and ("pause",) in prov.calls and a.tts.spoken == ["Pausando."] and not a.music.state.is_playing, str(a.tts.spoken))
    a2, ear2, calls2, _ = make(heard="x", text="Apolo, que horas são?")
    a2.music = MusicService(a2.cfg, providers={"fake": FakeProv()})
    a2.cfg.music_provider = "fake"
    with tempfile.TemporaryDirectory() as d:
        load_clips(a2, Path(d))
        a2._resume_listening()
        say(a2, utterance(1.4))
        wait_idle(a2)
    check("assistente: perguntas que não são música continuam no fluxo antigo (hora local)",
          bool(a2.tts.spoken) and any(k in a2.tts.spoken[0] for k in ("hora", "meio-dia", "meia-noite")), str(a2.tts.spoken))
    a3, _, _, _ = make(heard="x", text="Apolo")
    a3.cfg.music_enabled = False
    a3.cfg.wake_word_enabled = True
    a3.music = MusicService(a3.cfg, providers={"fake": FakeProv()})
    a3.cfg.music_provider = "fake"
    check("assistente: MUSIC_ENABLED=False -> 'toque X' não é interceptado", a3.music.parse("toque Believer") is None)


def main() -> int:
    t_intents()
    t_service()
    t_spotify()
    t_youtube()
    t_media_keys()
    t_decoder()
    t_player()
    t_player_race()
    t_local()
    t_assistant()
    print("\n" + ("Tudo certo." if not tf.FAILS else f"{len(tf.FAILS)} falha(s): " + ", ".join(tf.FAILS)))
    return 1 if tf.FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
