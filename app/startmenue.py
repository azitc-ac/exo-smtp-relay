"""Startmenü-Eintrag der Weboberfläche (nur Windows).

`install.ps1` legt „EXO SMTP Relay.url" an. Das TLS-Zertifikat des Dienstes trägt nur den
Hostnamen, nie „localhost" — der Link soll deshalb auf den Namen zeigen, den der Betreiber im
Assistenten (oder unter Einstellungen) setzt. Der Dienst läuft als SYSTEM und darf die Datei
im gemeinsamen Startmenü ändern. Fehlt sie (kein Windows-Installer), passiert nichts.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import config

log = logging.getLogger(__name__)


def _pfad() -> Path:
    basis = os.environ.get("ProgramData", r"C:\ProgramData")
    return Path(basis) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "EXO SMTP Relay.url"


def aktualisieren(hostname: str) -> bool:
    """Zeigt den Startmenü-Link auf https://<hostname>:<Port>/. True, wenn geändert."""
    hostname = (hostname or "").strip()
    if sys.platform != "win32" or not hostname:
        return False
    pfad = _pfad()
    if not pfad.exists():
        return False
    ziel = f"https://{hostname}{'' if config.WEBUI_PORT == 443 else f':{config.WEBUI_PORT}'}/"
    try:
        neu = f"[InternetShortcut]\r\nURL={ziel}\r\n".encode("ascii")   # Bytes: keine Zeilenenden-Umsetzung
        if pfad.read_bytes() == neu:
            return False
        pfad.write_bytes(neu)
        log.info("Startmenü-Link zeigt jetzt auf %s", ziel)
        return True
    except OSError as exc:
        log.warning("Startmenü-Link nicht aktualisiert: %s", exc)
        return False
