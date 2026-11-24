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


# ── Betriebszustände als Band auf JEDER Seite (wie Gateway 5bd5b1b) ──────────
# Bis v0.2.33 standen „Relay abgeschaltet“ und „keine Postfachadressen“ nur in
# der Übersicht (dashboard.html, zeichneWarnungen), Zustellfehler nur als Zahl
# in einer Kachel. Wer auf einer anderen Seite arbeitete, sah sie nicht — und
# ein vergessenes Abschalten weist stillschweigend jede Einlieferung ab.
# Jinja-Globals wie `lernmodus_rest_min`: keine Route kann sie vergessen.
# Erst nach abgeschlossener Einrichtung: Davor ist beides der Normalzustand,
# und der Assistent sagt selbst, was noch fehlt.

def _relay_aus_gemeldet() -> bool:
    """Relay abgeschaltet? Das Gegenstück zum Wartungsmodus des Gateways —
    nur dass hier nichts zurückgehalten, sondern alles abgewiesen wird."""
    try:
        return bool(settings_store.get("SETUP_COMPLETE")) \
            and not settings_store.get("SMTP_RELAY_ENABLED")
    except Exception:                                         # noqa: BLE001
        return False


templates.env.globals["relay_aus_gemeldet"] = _relay_aus_gemeldet


def _adressen_gemeldet() -> bool:
    """Keine Postfachadresse bekannt → `smtp_relay.pruefe` weist jede
    Einlieferung mit 451 ab. `known_addresses()` fragt nie selbst ab."""
    try:
        if not settings_store.get("SETUP_COMPLETE"):
            return False
        import exo_mailboxes
        return not exo_mailboxes.known_addresses()
    except Exception:                                         # noqa: BLE001
        return False


templates.env.globals["adressen_gemeldet"] = _adressen_gemeldet


def _zustellfehler_anzahl() -> int:
    """Einlieferungen, die Exchange Online seit der letzten erfolgreichen
    Zustellung nicht annahm (`mail_audit.zustellfehler_offen`). Das Relay hat
    keine Warteschlange: Das Gerät bekam 451 und versucht es selbst erneut."""
    try:
        import mail_audit
        return mail_audit.zustellfehler_offen()
    except Exception:                                         # noqa: BLE001
        return 0


templates.env.globals["zustellfehler_anzahl"] = _zustellfehler_anzahl


def _tls_ablauf_tage() -> int | None:
    """Resttage des TLS-Zertifikats, wenn es binnen `LE_RENEW_DAYS` abläuft
    (sonst None). Die Erinnerung für alles, was niemand selbst erneuert —
    DNS-01 von Hand, PFX — und der sichtbare Hinweis, wenn die automatische
    Erneuerung über certbot scheiterte (scheduler._tls_pruefen).
    Anders als die Bänder oben auch vor abgeschlossener Einrichtung: Ein
    ablaufendes Zertifikat ist nie der Normalzustand."""
    try:
        import tls_cert
        st = tls_cert.info()
        if not st.get("vorhanden") or st.get("fehler"):
            return None
        tage = int(st["tage_rest"])
        return tage if tage <= int(settings_store.get("LE_RENEW_DAYS") or 14) else None
    except Exception:                                         # noqa: BLE001
        return None


templates.env.globals["tls_ablauf_tage"] = _tls_ablauf_tage


def _update_verfuegbar() -> str | None:
    """Neuere Fassung im eingestellten Kanal (aus dem letzten Prüfergebnis,
    ohne Netz) — für das Band „Aktualisierung verfügbar“ auf jeder Seite."""
    try:
        import aktualisierung
        return aktualisierung.verfuegbare_version()
    except Exception:                                         # noqa: BLE001
        return None


templates.env.globals["update_verfuegbar"] = _update_verfuegbar


def _gateway_name() -> str:
    return settings_store.get("GATEWAY_NAME") or "EXO SMTP Relay"


class _NotAuthenticated(Exception):
    def __init__(self, is_api: bool = False):
        self.is_api = is_api


class _KennwortZuerst(Exception):
    """Angemeldet mit dem Auslieferungs-Kennwort — erst ändern (v0.2.25).

    Behandler in `app.py`: Seiten leiten zur Einrichtung (Schritt 1), API-
    Aufrufe bekommen 403 mit Hinweis."""

    def __init__(self, is_api: bool = False):
        self.is_api = is_api


# Was mit dem Auslieferungs-Kennwort erreichbar bleibt: die Einrichtungsseite
# (dort steht Schritt 1) und das Ändern selbst. Anders als im Gateway prüft die
# Einrichtungsseite ihre Anmeldung über `_check_auth`, deshalb steht sie hier.
NUR_MIT_VORGABE_KENNWORT = frozenset({"/einrichtung", "/api/password"})


def _vorgabe_kennwort_sperre(request: Request, user: str) -> None:
    """Angemeldet mit Vorgabe-Kennwort → nur Kennwort ändern.

    ⚠️ Bis v0.2.24 meldete das Auslieferungs-Kennwort (`admin`) voll an; der
    Hinweis darauf im Kopfbanner sperrte nichts. Wer ein frisch aufgesetztes
    Relay erreichte, bevor der Betreiber Schritt 1 abgeschlossen hatte, hatte
    die Verwaltung — und damit die Geräteliste, also wer Post einliefern darf.

    Das Relay kennt nur die örtliche Anmeldung; anders als im Gateway gibt es
    keine Entra-Kennung, die ausgenommen werden müsste.
    """
    if request.url.path in NUR_MIT_VORGABE_KENNWORT:
        return
    if _password_change_required():
        path = request.url.path
        raise _KennwortZuerst(is_api=path.startswith("/api/") or path.startswith("/log/"))


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


_DEFAULT_PASSWORDS = {"admin", "changeme", ""}


def _password_change_required() -> bool:
    """True, solange noch ein Vorgabe-/Platzhalterkennwort gilt (wie Gateway
    `hilfen._password_change_required`).

    Ein eigenes Kennwort über `WEBUI_PASSWORD` in der Umgebung zählt als
    geändert — sonst bliebe ein so eingerichtetes Relay dauerhaft gesperrt."""
    stored_hash = settings_store.get("ADMIN_PASSWORD_HASH") or ""
    if stored_hash:
        return False  # wurde geändert
    return config.WEBUI_PASSWORD in _DEFAULT_PASSWORDS


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
        _vorgabe_kennwort_sperre(request, user)
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
            _vorgabe_kennwort_sperre(request, credentials.username)
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
