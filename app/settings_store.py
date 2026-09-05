"""Einstellungen — eine JSON-Datei im Datenverzeichnis, mit Vorgaben.

Dieselbe Schnittstelle wie im grossen Gateway (`init`, `get`, `update`,
`public_view`, `nur_bekannte`), weil die gespiegelten Module (`smtp_relay.py`,
`relay_hosts.py`) genau diese Aufrufe machen. Was hier NICHT ist: Migrationen,
Umgebungs-Vorrang für Geheimnisse, 250 Schlüssel. Ein Relay hat zwei Dutzend.

⚠️ `REINJECT_MODE` steht hier fest auf `smtp` und ist nicht änderbar:
`smtp_relay.pruefe()` (gespiegelt) verweigert das Relay in jeder anderen
Betriebsart, weil nur der Smarthost-Weg fremde Absender unverändert
weiterreicht. Der Schlüssel bleibt deklariert, damit die Prüfung dort auch
hier gilt — und nicht still an einem fehlenden Schlüssel vorbeiläuft.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import RLock

import config
import secure_io

log = logging.getLogger(__name__)

SETTINGS_FILE = Path(config.DATA_DIR) / "settings.json"

DEFAULTS: dict = {
    # ── Betrieb ──────────────────────────────────────────────────────────────
    "GATEWAY_NAME": "EXO SMTP Relay",
    "LOG_LEVEL": "INFO",
    "LOG_RETENTION_DAYS": 30,
    "LOG_TIMEZONE": "Europe/Berlin",
    "WEBUI_USERNAME": "admin",
    "ADMIN_PASSWORD_HASH": "",
    "SESSION_SECRET": "",
    "PUBLIC_HOSTNAME": "",          # Hostname, unter dem Exchange den Dienst erreicht
    "SETUP_COMPLETE": False,        # Assistent abgeschlossen — vorher leitet "/" dorthin

    # ── Einrichtung über Entra (Assistent) ───────────────────────────────────
    "BOOTSTRAP_CLIENT_ID": "",      # eigene Login-App (Public Client) für den Assistenten
    "BOOTSTRAP_REDIRECT_URIS": [],  # dort registrierte Rückadressen (nach erstem Login bekannt)
    "TENANT_ID": "",
    "AZURE_APP_CREATED": False,     # App-Registrierung samt Zertifikat steht

    # ── Rückweg zu Exchange Online ───────────────────────────────────────────
    "EXO_SMARTHOST": "",            # <tenant>.mail.protection.outlook.com
    "EXO_PORT": 25,
    "EXO_SUBMIT_MODE": "smarthost", # "smarthost" (Port 25, Connector) | "submit" (Port 587, Konto)
    "SUBMIT_HOST": "smtp.office365.com",
    "SUBMIT_PORT": 587,
    "SUBMIT_USER": "",
    "SUBMIT_PASSWORD": "",
    "REINJECT_MODE": "smtp",        # FEST — siehe Modulkopf

    # ── Tenant und Adressquelle ──────────────────────────────────────────────
    "TENANT_DOMAIN": "",            # firma.onmicrosoft.com
    "CLIENT_ID": "",                # App-Registrierung (Exchange.ManageAsApp)
    # Postfachadressen von Hand — für Betreiber ohne App-Registrierung oder als
    # Ergänzung. ⚠️ Adressen, keine Domänen: `smtp_relay.pruefe()` (gespiegelt)
    # beurteilt Ziele an den ADRESSEN, und die Absenderdomänen leiten sich
    # daraus ab. Eine Domänenliste daneben wäre ein zweiter, laxerer Weg.
    "ADRESSEN_ZUSAETZLICH": [],
    "EXO_ABFRAGE_AN": True,         # Postfachliste stündlich per PowerShell holen
    "EXO_CONNECTOR_CREATED": False,

    # ── Relay ────────────────────────────────────────────────────────────────
    "SMTP_RELAY_ENABLED": True,
    "SMTP_RELAY_LERN_NETZE": [],
    "SMTP_RELAY_LERN_BIS": "",
    "SMTP_RELAY_EXTERN_VORGABE": False,
}

SECRET_KEYS = frozenset({
    "ADMIN_PASSWORD_HASH",
    "SESSION_SECRET",
    "SUBMIT_PASSWORD",
})

# Schlüssel, die nie über die Oberfläche geändert werden dürfen.
FIXED_KEYS = frozenset({"REINJECT_MODE"})

MASK = "••••••••"

_lock = RLock()
_data: dict = {}


def init(env_seed: dict | None = None) -> None:
    global _data
    with _lock:
        merged = dict(DEFAULTS)
        if env_seed:
            merged.update({k: v for k, v in env_seed.items() if k in DEFAULTS and v not in ("", None)})
        if SETTINGS_FILE.exists():
            try:
                merged.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
            except Exception as exc:                          # noqa: BLE001
                log.error("Einstellungen %s unlesbar: %s — versuche Sicherung", SETTINGS_FILE, exc)
                bak = SETTINGS_FILE.with_suffix(".bak")
                if bak.exists():
                    try:
                        merged.update(json.loads(bak.read_text(encoding="utf-8")))
                        log.warning("Einstellungen aus Sicherung %s geladen", bak)
                    except Exception as bak_exc:              # noqa: BLE001
                        log.error("Sicherung ebenfalls unlesbar: %s — Vorgaben", bak_exc)
        merged["REINJECT_MODE"] = "smtp"                      # fest, siehe Modulkopf
        _data = merged
        log.info("Einstellungen geladen (Datei vorhanden: %s)", SETTINGS_FILE.exists())


def get(key: str):
    with _lock:
        if not _data:
            init(config._ENV_SEEDS)
        return _data.get(key, DEFAULTS.get(key))


def get_all() -> dict:
    with _lock:
        if not _data:
            init(config._ENV_SEEDS)
        return dict(_data)


_TRUTHY = ("1", "true", "yes", "on")


def _coerce(key: str, value):
    """`str(False)` ist truthy — Schalter aus Formularen deshalb umsetzen."""
    default = DEFAULTS.get(key)
    if isinstance(default, bool) and not isinstance(value, bool):
        if isinstance(value, str):
            return value.strip().lower() in _TRUTHY
        if isinstance(value, (int, float)):
            return bool(value)
        return value
    if isinstance(default, int) and not isinstance(default, bool) \
            and isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    if isinstance(default, list) and isinstance(value, str):
        return [t.strip() for t in value.replace("\n", ",").split(",") if t.strip()]
    return value


def nur_bekannte(daten: dict) -> tuple[dict, list[str]]:
    """(übernommen, verworfen) — Unbekanntes und Festes wird nicht still geschluckt."""
    if not isinstance(daten, dict):
        return {}, []
    uebernommen = {k: v for k, v in daten.items() if k in DEFAULTS and k not in FIXED_KEYS}
    verworfen = sorted(set(daten) - set(uebernommen))
    return uebernommen, verworfen


def update(patch: dict) -> None:
    with _lock:
        if not _data:
            init(config._ENV_SEEDS)
        _data.update({k: _coerce(k, v) for k, v in patch.items()
                      if k in DEFAULTS and k not in FIXED_KEYS})
        _save()


def _save() -> None:
    with _lock:
        to_write = {k: _data.get(k, DEFAULTS[k]) for k in DEFAULTS}
        if SETTINGS_FILE.exists():
            bak = SETTINGS_FILE.with_suffix(".bak")
            try:
                secure_io.write_secret_bytes(bak, SETTINGS_FILE.read_bytes())
            except OSError as exc:
                log.warning("Sicherung %s nicht geschrieben: %s", bak, exc)
        # settings.json enthält Passwörter → 600, Verzeichnis 700, atomar.
        secure_io.write_secret_json(SETTINGS_FILE, to_write)
        log.info("Einstellungen gespeichert: %s", SETTINGS_FILE)


def public_view() -> dict:
    """Alle Einstellungen mit maskierten Geheimnissen — für Vorlagen."""
    aus = get_all()
    for k in SECRET_KEYS:
        if aus.get(k):
            aus[k] = MASK
    return aus
