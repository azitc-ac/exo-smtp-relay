"""PKCE-Anmeldung an Entra für den Einrichtungsassistenten.

Übernommen aus dem Gateway: Der Betreiber meldet sich einmal als Entra-
Administrator an; mit dem so erhaltenen Token legt `setup_wizard` die
App-Registrierung an, erteilt die Zustimmung und lädt das Zertifikat hoch.

Als Login-App dient eine eigene „Bootstrap-App" (Public Client, PKCE ohne
Geheimnis) — `BOOTSTRAP_CLIENT_ID`. Wer das grosse Gateway betreibt, trägt
dessen Login-App ein und ergänzt dort die Rückadresse dieses Dienstes. Ohne
eigene App fällt der Ablauf auf Microsofts Graph-CLI-App zurück, die nur die
Localhost-Rückadresse zulässt (Copy-Paste-Weg).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
import urllib.parse

log = logging.getLogger(__name__)

_FALLBACK_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"     # Microsoft Graph CLI App

# Delegierte Rechte, um App-Registrierungen anzulegen und Zustimmung zu erteilen.
BOOTSTRAP_SCOPES = [
    "https://graph.microsoft.com/Application.ReadWrite.All",
    "https://graph.microsoft.com/AppRoleAssignment.ReadWrite.All",
    "https://graph.microsoft.com/Directory.ReadWrite.All",
    "https://graph.microsoft.com/RoleManagement.ReadWrite.Directory",
    "offline_access",
]

_sessions: dict[str, dict] = {}
_SESSION_TTL = 600


def _get_client_id() -> str:
    import settings_store
    custom = (settings_store.get("BOOTSTRAP_CLIENT_ID") or "").strip()
    return custom or _FALLBACK_CLIENT_ID


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))
    return verifier, _b64url(hashlib.sha256(verifier.encode()).digest())


def create_session(redirect_uri: str) -> tuple[str, str]:
    """Neue Sitzung → (state, Anmeldeadresse)."""
    _prune_sessions()
    state = secrets.token_urlsafe(24)
    verifier, challenge = generate_pkce_pair()
    _sessions[state] = {"verifier": verifier, "redirect_uri": redirect_uri,
                        "created_at": time.monotonic()}
    params = {
        "client_id": _get_client_id(), "response_type": "code", "redirect_uri": redirect_uri,
        "scope": " ".join(BOOTSTRAP_SCOPES), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account",
    }
    return state, ("https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?"
                   + urllib.parse.urlencode(params))


def pop_session(state: str) -> dict | None:
    _prune_sessions()
    sitzung = _sessions.pop(state, None)
    if sitzung is None:
        log.warning("PKCE-Sitzung zu state=%s nicht gefunden", state)
    return sitzung


def _prune_sessions() -> None:
    jetzt = time.monotonic()
    for s in [s for s, v in _sessions.items() if jetzt - v["created_at"] > _SESSION_TTL]:
        del _sessions[s]


async def exchange_code(code: str, verifier: str, redirect_uri: str) -> dict:
    """Code gegen Token tauschen. Wirft RuntimeError."""
    import httpx
    daten = {
        "client_id": _get_client_id(), "grant_type": "authorization_code", "code": code,
        "redirect_uri": redirect_uri, "code_verifier": verifier,
        "scope": " ".join(BOOTSTRAP_SCOPES),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post("https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
                                 data=daten)
    body = resp.json()
    if "access_token" not in body:
        raise RuntimeError("Token-Austausch fehlgeschlagen: "
                           + (body.get("error_description") or body.get("error") or str(body)))
    log.info("PKCE-Token-Austausch erfolgreich")
    return body
