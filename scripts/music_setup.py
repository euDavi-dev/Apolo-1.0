"""Configuração dos serviços de música do APOLO (rode uma vez).

  python scripts/music_setup.py status            # o que está pronto e o que falta
  python scripts/music_setup.py spotify           # conecta a conta Spotify (abre o navegador; OAuth PKCE, sem segredo)
  python scripts/music_setup.py youtube           # confere a chave YOUTUBE_API_KEY com uma busca de teste (custa 100 unidades)
  python scripts/music_setup.py local [pasta]     # indexa a pasta de músicas e mostra quantas faixas achou

Com contas cadastradas, entre na sua conta local; as chaves vêm do cofre. Sem contas, usa .env. Nada é enviado para fora além das APIs oficiais."""
from __future__ import annotations

import os
import sys
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config
from app.core.accounts import AccountStore, AccountError, activate, credential
from app.services.music_intents import MusicIntent  # noqa: E402
from app.services.music_providers import LocalProvider, MusicError, SpotifyProvider, YouTubeProvider  # noqa: E402


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    store = AccountStore(config.DATA_DIR)
    if store.has_accounts():
        try:
            activate(store.login(input("Usuário do Apolo: "), getpass("Senha: ")))
        except (AccountError, OSError):
            print("Não foi possível entrar na conta.")
            return 1
    cfg = config.Settings.load()
    sp = SpotifyProvider(credential("SPOTIFY_CLIENT_ID"), config.DATA_DIR / "spotify_token.json")
    yt = YouTubeProvider(credential("YOUTUBE_API_KEY"), config.DATA_DIR / "youtube_cache.json")
    lp = LocalProvider(sys.argv[2] if cmd == "local" and len(sys.argv) > 2 else (cfg.music_local_dir or Path.home() / "Music"))
    try:
        if cmd == "spotify":
            print("Abrindo o navegador para você autorizar o APOLO no Spotify (redirect: http://127.0.0.1:8888/callback)...")
            print("Pré-requisitos: conta Premium e um app em https://developer.spotify.com/dashboard com essa Redirect URI.")
            sp.login()
            print("Spotify conectado. Abra o Spotify neste computador e diga: \"Apolo, toque Believer\".")
        elif cmd == "youtube":
            ok, why = yt.available()
            if not ok:
                print("YouTube não está pronto:", why)
                return 1
            r = yt._search("imagine dragons believer")
            print(f"Chave OK. Primeiro resultado: {r[0]['title']!r}" if r else "Chave OK, mas sem resultados.")
        elif cmd == "local":
            print(f"{lp.scan()} faixas em {lp.dir}")
        else:
            for p in (sp, yt):
                ok, why = p.available()
                print(f"{p.name:8} {'PRONTO' if ok else 'não configurado'}  {why}")
            lp.scan()
            ok, why = lp.available()
            print(f"{'local':8} {'PRONTO' if ok else 'não configurado'}  {why or str(len(lp.index)) + ' faixas'}")
            print(f"\nServiço escolhido nas Configurações: {cfg.music_provider}")
    except MusicError as e:
        print("ERRO:", e.spoken)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
