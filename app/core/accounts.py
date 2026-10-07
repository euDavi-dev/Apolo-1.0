"""Contas locais: scrypt + AES-GCM; nenhuma senha ou chave em texto puro."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SECRET_NAMES = ("GEMINI_API_KEY", "YOUTUBE_API_KEY", "SPOTIFY_CLIENT_ID")


class AccountError(ValueError):
    pass


def username_id(username: str) -> str:
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,40}", username):
        raise AccountError("Use de 3 a 40 letras sem acento, números, ponto, hífen ou _ no usuário.")
    if username.endswith(".") or username.split(".")[0] in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}:
        raise AccountError("Este nome é reservado pelo sistema. Escolha outro usuário.")
    return username


def validate_profile(name: str, city: str) -> None:
    if not 2 <= len(name.strip()) <= 80:
        raise AccountError("Informe seu nome com 2 a 80 caracteres.")
    if not 2 <= len(city.strip()) <= 100:
        raise AccountError("Informe sua cidade com 2 a 100 caracteres.")


def derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)


@dataclass
class Session:
    store: "AccountStore"
    username: str
    key: bytes = field(repr=False)
    salt: bytes = field(repr=False)
    data: dict = field(repr=False)

    @property
    def directory(self) -> Path:
        return self.store.root / "profiles" / self.username

    def save(self, data: dict) -> None:
        validate_profile(data["name"], data["city"])
        self.store.write(self.username, self.key, self.salt, data)
        self.data = data


class AccountStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, username: str) -> Path:
        return self.root / "accounts" / (username_id(username) + ".json")

    def has_accounts(self) -> bool:
        return any((self.root / "accounts").glob("*.json"))

    def create(self, username: str, password: str, name: str, city: str) -> Session:
        username = username_id(username)
        validate_profile(name, city)
        if not 10 <= len(password) <= 256:
            raise AccountError("Escolha uma senha de 10 a 256 caracteres.")
        if self.path(username).exists():
            raise AccountError("Este usuário já existe. Entre na conta ou escolha outro nome.")
        salt = os.urandom(16)
        session = Session(self, username, derive_key(password, salt), salt,
                          {"name": name.strip(), "city": city.strip(), "secrets": {}, "tutorial_done": False})
        session.save(session.data)
        return session

    def login(self, username: str, password: str) -> Session:
        username = username_id(username)
        try:
            envelope = json.loads(self.path(username).read_text(encoding="utf-8"))
            if not isinstance(envelope, dict) or envelope.get("version") != 1:
                raise ValueError("unsupported version")
            salt = base64.b64decode(envelope["salt"], validate=True)
            key = derive_key(password, salt)
            data = json.loads(AESGCM(key).decrypt(
                base64.b64decode(envelope["nonce"], validate=True),
                base64.b64decode(envelope["vault"], validate=True), username.encode()))
            if not isinstance(data, dict) or not isinstance(data.get("secrets"), dict):
                raise ValueError("invalid vault")
            validate_profile(data["name"], data["city"])
            return Session(self, username, key, salt, data)
        except (FileNotFoundError, InvalidTag):
            raise AccountError("Usuário ou senha incorretos.") from None
        except (ValueError, KeyError, TypeError):
            raise AccountError("Não foi possível ler esta conta. Restaure seu backup local.") from None

    def write(self, username: str, key: bytes, salt: bytes, data: dict) -> None:
        path = self.path(username)
        path.parent.mkdir(parents=True, exist_ok=True)
        nonce = os.urandom(12)
        vault = AESGCM(key).encrypt(nonce, json.dumps(data, ensure_ascii=False).encode(), username.encode())
        envelope = {"version": 1, **{k: base64.b64encode(v).decode()
                                     for k, v in (("salt", salt), ("nonce", nonce), ("vault", vault))}}
        fd, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(envelope, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)


def activate(session: Session) -> None:
    """Antes de importar os serviços, isola todos os seus caminhos de dados."""
    from app.core import config
    config.DATA_DIR = session.directory
    config.SETTINGS_FILE = session.directory / "settings.json"
    config.COMMANDS_FILE = session.directory / "commands.json"
    if getattr(sys, "frozen", False):
        config.ACTIVATION_DIR = session.directory / "sounds" / "activation"
    config.ACTIVE_SESSION = session


def credential(name: str) -> str:
    from app.core import config
    session = getattr(config, "ACTIVE_SESSION", None)
    if session is not None:
        return session.data["secrets"].get(name, "").strip()
    return os.getenv(name, "").strip()  # compatibilidade com scripts fora do app
