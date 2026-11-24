"""Umgebung des Windows-Dienstes (Registry) ZUSAMMENFÜHREN statt überschreiben.

Bis 0.2.43 schrieb `install.ps1` den Wert `Environment` des Dienstes jedesmal neu:
nur PYTHONPATH und WEBUI_PORT. Ein Upgrade verlor dadurch eigene Einträge (etwa
WEBUI_PASSWORD) und setzte ohne `-WebPort` einen eigenen Port auf 8443 zurück.
Bestehende Installationen ohne nachgeladene Wurzelzertifikate MÜSSEN für 0.2.43+
einmal install.ps1 ausführen — genau dieser Weg darf nichts verlieren.

Jetzt: Der Installer setzt nur die Schlüssel, die er verantwortet (PYTHONPATH,
WEBUI_PORT); alles andere bleibt in Reihenfolge erhalten. Der Port kommt aus
`--port` (ausdrücklich `-WebPort`), sonst aus der bestehenden Umgebung, 8443 nur
bei der Erstinstallation. Einen Port-Schlüssel in settings.json gibt es nicht.

Aufruf durch install.ps1 (als Administrator, nach `service.py install/update`):
    python dienst_umgebung.py --setzen PYTHONPATH=<...> [--port 9443]
Ausgabe auf stdout: der wirksame Web-Port (letzte Zeile). Exit 0 bei Erfolg,
sonst 1 mit Grund auf stdout (stderr bricht PowerShell 5.1 sonst ab).
"""
from __future__ import annotations

import sys
from typing import Callable

SCHLUESSEL = r"SYSTEM\CurrentControlSet\Services\ExoSmtpRelay"
WERT = "Environment"
VORGABE_PORT = 8443


def _name(eintrag: str) -> str:
    return eintrag.split("=", 1)[0].strip().upper()      # Windows: Namen ohne Gross/Klein


def zusammenfuehren(alt: list[str], setzen: dict[str, str]) -> list[str]:
    """Vorhandene Einträge behalten, die Installer-Schlüssel setzen bzw. ersetzen."""
    offen = {k.upper(): (k, v) for k, v in setzen.items()}
    neu: list[str] = []
    for eintrag in alt:
        if not eintrag:
            continue
        name = _name(eintrag)
        if name in offen:
            k, v = offen.pop(name)
            neu.append(f"{k}={v}")
        elif name in {k.upper() for k in setzen}:
            continue                                     # Doppelter Installer-Schlüssel
        else:
            neu.append(eintrag)
    neu.extend(f"{k}={v}" for k, v in offen.values())
    return neu


def port_bestimmen(alt: list[str], ausdruecklich: int | None) -> int:
    if ausdruecklich is not None:
        return ausdruecklich
    for eintrag in reversed(alt):
        if _name(eintrag) == "WEBUI_PORT":
            wert = eintrag.split("=", 1)[1].strip() if "=" in eintrag else ""
            if wert.isdigit() and 0 < int(wert) < 65536:
                return int(wert)
    return VORGABE_PORT


def _registry_lesen() -> list[str]:                     # pragma: no cover (nur Windows)
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SCHLUESSEL) as k:
            wert, typ = winreg.QueryValueEx(k, WERT)
    except FileNotFoundError:
        return []
    if typ == winreg.REG_MULTI_SZ:
        return list(wert or [])
    return [str(wert)] if wert else []


def _registry_schreiben(eintraege: list[str]) -> None:  # pragma: no cover (nur Windows)
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SCHLUESSEL, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, WERT, 0, winreg.REG_MULTI_SZ, eintraege)


def main(argv: list[str], lesen: Callable[[], list[str]] = _registry_lesen,
         schreiben: Callable[[list[str]], None] = _registry_schreiben) -> int:
    setzen: dict[str, str] = {}
    port: int | None = None
    try:
        args = list(argv[1:])
        while args:
            schalter = args.pop(0)
            if schalter == "--setzen":
                k, v = args.pop(0).split("=", 1)
                setzen[k] = v
            elif schalter == "--port":
                port = int(args.pop(0))
                if not 0 < port < 65536:
                    raise ValueError(f"Port {port} ausserhalb 1-65535")
            else:
                raise ValueError(f"unbekannter Schalter {schalter}")
        alt = lesen()
        wirksam = port_bestimmen(alt, port)
        setzen["WEBUI_PORT"] = str(wirksam)
        schreiben(zusammenfuehren(alt, setzen))
    except Exception as exc:                             # noqa: BLE001
        print(f"Fehler: {exc}")
        return 1
    print(wirksam)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
