"""Startmenü-Eintrag der Weboberfläche (nur Windows).

`install.ps1` legt „EXO SMTP Relay.url" an. Das TLS-Zertifikat des Dienstes trägt nur den
Hostnamen, nie „localhost" — der Link soll deshalb auf den Hostnamen zeigen, den der Betreiber im
Assistenten (oder unter Einstellungen) setzt, SOFERN dieser auf dem Server selbst auflöst.
Der Hostname ist der Name, unter dem Exchange den Dienst von aussen erreicht; hier muss er nicht
auflösbar sein (Split-DNS, NAT). Dann zeigt der Link auf den Rechnernamen, zuletzt auf localhost.
Der Dienst läuft als SYSTEM und darf die Datei im gemeinsamen Startmenü ändern. Fehlt sie (kein
Windows-Installer), passiert nichts.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
from pathlib import Path

import config

log = logging.getLogger(__name__)


def _pfad() -> Path:
    basis = os.environ.get("ProgramData", r"C:\ProgramData")
    return Path(basis) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "EXO SMTP Relay.url"


def _loest_auf(name: str) -> bool:
    try:
        socket.getaddrinfo(name, None)
        return True
    except OSError:
        return False


def _zielname(hostname: str) -> str:
    for kandidat in (hostname, socket.gethostname()):
        if kandidat and _loest_auf(kandidat):
            return kandidat
    return "localhost"


def aktualisieren(hostname: str) -> bool:
    """Zeigt den Startmenü-Link auf https://<Name>:<Port>/. True, wenn geändert."""
    hostname = (hostname or "").strip()
    if sys.platform != "win32" or not hostname:
        return False
    pfad = _pfad()
    if not pfad.exists():
        return False
    port = "" if config.WEBUI_PORT == 443 else f":{config.WEBUI_PORT}"
    ziel = f"https://{_zielname(hostname)}{port}/"
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
