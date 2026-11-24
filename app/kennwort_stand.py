"""Gilt noch das Auslieferungs-Kennwort? — EINE Regel für Oberfläche und Installer.

Die Weboberfläche (`webui.deps._password_change_required`) sperrt, solange kein
eigenes Kennwort gesetzt ist. `windows/install.ps1` meldete bis 0.2.43 am Ende
IMMER „admin / admin - bitte aendern“, auch nach einem Upgrade mit längst
geändertem Kennwort (Labtest 0.2.43). Beide fragen jetzt diese Datei.

Eigenes Kennwort = `ADMIN_PASSWORD_HASH` in settings.json gesetzt, oder ein
eigenes `WEBUI_PASSWORD` in der Umgebung (kein Vorgabe-/Platzhalterwert).

Ohne Abhängigkeiten außer der Standardbibliothek: install.ps1 ruft das Modul
direkt mit dem Python der venv auf, vor dem ersten Dienststart.
    python kennwort_stand.py <pfad\\zu\\settings.json>
Ausgabe: `eigenes`, `vorgabe` oder `unbekannt` — immer auf stdout und mit Exit 0
(Windows PowerShell 5.1 macht aus stderr eines Programms bei
`$ErrorActionPreference = "Stop"` einen Abbruch des Installers).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

VORGABE_KENNWOERTER = frozenset({"admin", "changeme", ""})


def aenderung_noetig(gespeicherter_hash: str, umgebungs_kennwort: str | None) -> bool:
    """True, solange noch ein Vorgabe-/Platzhalterkennwort gilt."""
    if gespeicherter_hash:
        return False
    return (umgebungs_kennwort or "") in VORGABE_KENNWOERTER


def hash_aus_datei(settings_datei: Path) -> str:
    """`ADMIN_PASSWORD_HASH` aus settings.json; leer, wenn es die Datei (noch) nicht gibt.
    Unlesbar → OSError/ValueError an den Aufrufer (kein stilles „vorgabe“)."""
    if not settings_datei.is_file():
        return ""
    daten = json.loads(settings_datei.read_text(encoding="utf-8"))
    return str(daten.get("ADMIN_PASSWORD_HASH") or "") if isinstance(daten, dict) else ""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("unbekannt")
        return 0
    try:
        gespeichert = hash_aus_datei(Path(argv[1]))
    except (OSError, ValueError):
        print("unbekannt")
        return 0
    noetig = aenderung_noetig(gespeichert, os.environ.get("WEBUI_PASSWORD", "admin"))
    print("vorgabe" if noetig else "eigenes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
