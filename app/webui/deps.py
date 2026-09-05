"""Gemeinsames Fundament der Weboberfläche — Anmeldung, Vorlagen, Protokoll.

Dieselbe Richtung wie im Gateway:  app.py → routen/*.py → deps.py.
Diese Datei importiert kein Routenmodul und nicht `webui.app`.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import logging
import queue as _queue_mod
import secrets
import threading
import time as _time
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

import config
import settings_store
import sitzung

log = logging.getLogger("webui")

security = HTTPBasic(auto_error=False)

_STATIC_DIR = Path(__file__).parent / "static"
_TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
templates.env.globals["version"] = config.VERSION


def _lernmodus_rest_min() -> int:
    """Restminuten des Lernmodus — als Jinja-Global, damit das Banner auf JEDER
    Seite steht und keine Route es vergessen kann."""
    try:
        import smtp_relay
        from datetime import datetime, timezone
        bis = smtp_relay.lernmodus_bis()
        if not bis:
            return 0
        return max(1, round((bis - datetime.now(timezone.utc)).total_seconds() / 60))
    except Exception:                                         # noqa: BLE001
        return 0


templates.env.globals["lernmodus_rest_min"] = _lernmodus_rest_min


def _gateway_name() -> str:
    return settings_store.get("GATEWAY_NAME") or "EXO SMTP Relay"


class _NotAuthenticated(Exception):
    def __init__(self, is_api: bool = False):
        self.is_api = is_api


# ── Passwörter ────────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return f"pbkdf2:sha256:{salt}:{key.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, alg, salt, key_hex = stored.split(":", 3)
        assert alg == "sha256"
    except Exception:                                         # noqa: BLE001
        return False
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return hmac.compare_digest(key.hex(), key_hex)


def _check_password(password: str) -> bool:
    stored_hash = settings_store.get("ADMIN_PASSWORD_HASH") or ""
    if stored_hash:
        return _verify_password(password, stored_hash)
    if not password or not config.WEBUI_PASSWORD:
        return False
    return secrets.compare_digest(password.encode(), config.WEBUI_PASSWORD.encode())


def _password_change_required() -> bool:
    """Solange kein eigenes Passwort gesetzt ist, gilt `admin` — das darf nicht
    still so bleiben."""
    return not (settings_store.get("ADMIN_PASSWORD_HASH") or "")


templates.env.globals["password_change_required"] = _password_change_required


# ── Anmeldung ─────────────────────────────────────────────────────────────────

def _get_session_user(request: Request) -> str | None:
    cookie = request.cookies.get(sitzung.SESSION_COOKIE)
    if not cookie:
        return None
    payload = sitzung.verify_session_cookie(cookie)
    return payload.get("u") if payload else None


def _check_auth(request: Request,
                credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Sitzungskeks → HTTP-Basic (Notzugang, gedrosselt) → 401 bzw. Weiterleitung."""
    user = _get_session_user(request)
    if user:
        return user
    if credentials and credentials.username and credentials.password:
        import login_drossel
        ip = request.client.host if request.client else "?"
        if login_drossel.gesperrt(ip):
            raise HTTPException(429, "Zu viele Fehlversuche — bitte kurz warten.")
        username = settings_store.get("WEBUI_USERNAME") or "admin"
        if (secrets.compare_digest(credentials.username.encode(), username.encode())
                and _check_password(credentials.password)):
            login_drossel.erfolg(ip)
            return credentials.username
        login_drossel.fehlversuch(ip)
        log.warning("Fehlgeschlagene Basic-Anmeldung von %s", ip)
    path = request.url.path
    raise _NotAuthenticated(is_api=path.startswith("/api/") or path.startswith("/log/"))


# Es gibt nur eine Rolle. Der Name bleibt, damit Routen wie im Gateway lesen.
_require_admin = _check_auth


# ── Protokollstrom im Arbeitsspeicher ────────────────────────────────────────
_LOG_BUFFER: collections.deque = collections.deque(maxlen=500)
_LOG_SUBSCRIBERS: list[_queue_mod.Queue] = []
_LOG_SUBSCRIBERS_LOCK = threading.Lock()


class _MemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        _LOG_BUFFER.append(line)
        with _LOG_SUBSCRIBERS_LOCK:
            for q in _LOG_SUBSCRIBERS:
                try:
                    q.put_nowait(line)
                except _queue_mod.Full:
                    pass


_mem_handler = _MemoryLogHandler()
_mem_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_mem_handler)

_LOG_TOKENS: dict[str, float] = {}


def _make_log_token() -> str:
    token = secrets.token_urlsafe(32)
    _LOG_TOKENS[token] = _time.time() + 3600
    for k in [k for k, exp in _LOG_TOKENS.items() if _time.time() > exp]:
        _LOG_TOKENS.pop(k, None)
    return token


def _check_log_token(token: str) -> bool:
    exp = _LOG_TOKENS.get(token)
    return exp is not None and _time.time() < exp
