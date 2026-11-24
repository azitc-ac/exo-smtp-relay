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


# ── Widerruf (seit v0.2.26, wie Gateway v1.11.3) ────────────────────────────
# Ein Sitzungsmerkmal galt bis v0.2.25 seine vollen 8 Stunden — auch nachdem
# das Kennwort geändert war. Wer das Kennwort ändert, weil es bekannt geworden
# ist, sperrte damit niemanden aus. Jetzt trägt jedes Merkmal die Kennung des
# gerade gültigen Kennworts (`k`), und `verify_session_cookie` prüft sie bei
# JEDER Anfrage: Ein Kennwortwechsel (über jeden Weg, auch die Umgebung) macht
# alle älteren Sitzungen ungültig.

def _kennwort_kennung() -> str:
    """Kurze Kennung des gültigen örtlichen Kennworts — kein Rückschluss darauf."""
    import hashlib
    import config
    grundlage = (settings_store.get("ADMIN_PASSWORD_HASH")
                 or "env:" + (getattr(config, "WEBUI_PASSWORD", "") or ""))
    return hashlib.sha256(("sitzung|" + grundlage).encode()).hexdigest()[:16]


def create_session_cookie(user: str) -> str:
    return URLSafeTimedSerializer(_secret()).dumps(
        {"u": user, "ts": int(time.time()), "k": _kennwort_kennung()})


def _noch_gueltig(payload: dict) -> bool:
    return payload.get("k") == _kennwort_kennung()


def verify_session_cookie(value: str) -> dict | None:
    """Prüft und entschlüsselt ein Sitzungsmerkmal. None auch dann, wenn die
    Sitzung inzwischen widerrufen ist (`_noch_gueltig`)."""
    try:
        payload = URLSafeTimedSerializer(_secret()).loads(value, max_age=SESSION_TTL)
    except (BadSignature, SignatureExpired):
        return None
    except Exception as exc:                                  # noqa: BLE001
        log.debug("Sitzungskeks: %s", exc)
        return None
    if not isinstance(payload, dict) or not _noch_gueltig(payload):
        return None
    return payload
