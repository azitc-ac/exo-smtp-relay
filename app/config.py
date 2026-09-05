"""Feste Betriebsparameter — aus der Umgebung, mit plattformneutralen Vorgaben.

Der Dienst läuft an drei Orten: im Container (Linux, amd64/arm64), als
systemd-Dienst und als Windows-Dienst. Die Pfade dürfen deshalb nirgends als
Literal stehen — das war im grossen Gateway die Lehre aus 30 Stellen mit
dem festen Containerpfad (siehe dortiges `config.py`).

VORGABE FÜR DAS DATENVERZEICHNIS
--------------------------------
Ohne `DATA_DIR` liegt es NEBEN dem Anwendungsverzeichnis (`../data`). Das ist
der Ort, den der Windows-Installer und die systemd-Unit anlegen; der Container
setzt `DATA_DIR=/app/data` ausdrücklich. Ein Vorgabewert wie `/app/data` liefe
unter Windows ins Leere und legte im besten Fall `C:\\app\\data` an.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def _optional(name: str, default: str = "") -> str:
    # Ein GESETZTES, aber leeres Env-Var (docker-compose `${VAR:-}`) muss wie
    # „nicht gesetzt" gelten — sonst wird aus `int("")` ein Absturz beim Start.
    return os.environ.get(name) or default


DATA_DIR = _optional("DATA_DIR", str(APP_DIR.parent / "data"))

# ── Listener ──────────────────────────────────────────────────────────────────
SMTP_PORT = int(_optional("SMTP_PORT", "25"))
SMTP_BIND = _optional("SMTP_BIND", "0.0.0.0")
WEBUI_PORT = int(_optional("WEBUI_PORT", "8080"))
WEBUI_BIND = _optional("WEBUI_BIND", "0.0.0.0")

# TLS-Zertifikat des Listeners. Fehlt es, erzeugt `tls_cert.sicherstellen()`
# beim Start ein selbstsigniertes — Geräte im eigenen Netz prüfen den
# Aussteller ohnehin nicht, und ohne Zertifikat gäbe es gar kein STARTTLS.
SMTP_TLS_CERT = _optional("SMTP_TLS_CERT", str(Path(DATA_DIR) / "certs" / "cert.pem"))
SMTP_TLS_KEY = _optional("SMTP_TLS_KEY", str(Path(DATA_DIR) / "certs" / "key.pem"))

# ── Web-UI ────────────────────────────────────────────────────────────────────
WEBUI_PASSWORD = _optional("WEBUI_PASSWORD", "admin")

# ── PowerShell ────────────────────────────────────────────────────────────────
# Das Exchange-Online-Modul läuft unter PowerShell 7 (`pwsh`, Container und
# Linux) UND unter Windows PowerShell 5.1 (`powershell.exe`). Bevorzugt wird
# pwsh; fehlt es, greift unter Windows die eingebaute 5.1. `PWSH` in der
# Umgebung schlägt beides.
def _powershell() -> str:
    eigen = _optional("PWSH")
    if eigen:
        return eigen
    if shutil.which("pwsh"):
        return "pwsh"
    if sys.platform == "win32" and shutil.which("powershell"):
        return "powershell"
    return "pwsh"                      # fehlt es, sagt es der Aufruf deutlich


PWSH = _powershell()

# ── Startwerte für settings.json (nur beim allerersten Start) ─────────────────
_ENV_SEEDS: dict = {
    "LOG_LEVEL": _optional("LOG_LEVEL", "INFO").upper(),
    "WEBUI_USERNAME": _optional("WEBUI_USERNAME", "admin"),
    "EXO_SMARTHOST": _optional("EXO_SMARTHOST", ""),
    "TENANT_DOMAIN": _optional("TENANT_DOMAIN", ""),
    "CLIENT_ID": _optional("CLIENT_ID", ""),
}


# ── Version ───────────────────────────────────────────────────────────────────
def _read_version() -> str:
    for path in (APP_DIR / "VERSION", APP_DIR.parent / "VERSION"):
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
    return "dev"


VERSION = _read_version()
