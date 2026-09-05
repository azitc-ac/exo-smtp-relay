"""Sitzungskeks — signiert mit itsdangerous, Geheimnis in den Einstellungen.

Nur die örtliche Anmeldung (Benutzername + Passwort). Kein Microsoft-Login:
Ein Relay steht im eigenen Netz, wird von einer Person verwaltet, und jede
Anbindung an Entra wäre hier mehr Angriffsfläche als Nutzen.
"""
from __future__ import annotations

import logging
import secrets
import time

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import settings_store

log = logging.getLogger(__name__)

SESSION_COOKIE = "relay_session"
SESSION_TTL = 8 * 3600


def _secret() -> str:
    geheim = settings_store.get("SESSION_SECRET") or ""
    if not geheim:
        geheim = secrets.token_hex(32)
        settings_store.update({"SESSION_SECRET": geheim})
    return geheim


def create_session_cookie(user: str) -> str:
    return URLSafeTimedSerializer(_secret()).dumps({"u": user, "ts": int(time.time())})


def verify_session_cookie(value: str) -> dict | None:
    try:
        return URLSafeTimedSerializer(_secret()).loads(value, max_age=SESSION_TTL)
    except (BadSignature, SignatureExpired):
        return None
    except Exception as exc:                                  # noqa: BLE001
        log.debug("Sitzungskeks: %s", exc)
        return None
