#!/usr/bin/env python3
"""lock_hashes — SHA-256-Prüfsummen in app/requirements.lock eintragen und prüfen.

Installiert wird überall mit `pip install --require-hashes --only-binary :all: -r requirements.lock`
(install.ps1, systemd-Einrichtung, Dockerfile, update_tausch.phase_pip, CI). pip
lädt dann nur Dateien, deren SHA-256 hier steht — ein ausgetauschtes Paket auf
PyPI oder einem Spiegel fällt auf, statt installiert zu werden.

WELCHE DATEIEN
    Nur Wheels (keine Quellpakete: ein Quellpaket führt beim Kunden Build-Code
    aus), und davon die für die Zielplattformen:

        Python      3.11, 3.12, 3.13 (CPython, ohne free-threaded „t“)
        Plattform   win_amd64, manylinux x86_64, manylinux aarch64
                    sowie plattformunabhängige Wheels (any)

    Kein win_arm64: cryptography liefert dafür kein Wheel (Stand 50.0.0). Windows
    auf ARM nimmt die x64-Fassung von Python (Emulation), siehe install.ps1.

    Alle glibc-Stufen (manylinux1 … manylinux_2_X), denn pip wählt auf einem
    neueren System die höchste passende — deren Prüfsumme muss dann hier stehen.

QUELLE
    Die JSON-Schnittstelle von PyPI (https://pypi.org/pypi/<paket>/<fassung>/json)
    nennt zu jeder Datei ihre SHA-256. Die FASSUNGEN kommen nicht von hier,
    sondern aus der Lock-Datei (Gateway-Stand, siehe deren Kopf) — dieses
    Werkzeug ändert keine Fassung, es ergänzt nur Prüfsummen.

AUFRUF
    python3 tools/lock_hashes.py schreiben    # Prüfsummen neu eintragen (Netz)
    python3 tools/lock_hashes.py pruefen      # Datei == PyPI-Stand? (Netz, CI)
    python3 tools/lock_hashes.py abdeckung    # je Plattform/Python: fehlt ein Wheel?
    python3 tools/lock_hashes.py anforderungen <ziel> --plattform windows|linux
        # Lock-Datei OHNE Umgebungsmarken für `pip download --platform` (pip wertet
        # Marken gegen den LAUFENDEN Rechner aus, nicht gegen die Zielplattform)
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
LOCK = WURZEL / "app" / "requirements.lock"

PYTHON = ("311", "312", "313")
PLATTFORMEN = {
    "win_amd64": re.compile(r"^win_amd64$"),
    "linux_x86_64": re.compile(r"^manylinux(1|2010|2014|_\d+_\d+)_x86_64$"),
    "linux_aarch64": re.compile(r"^manylinux(2014|_\d+_\d+)_aarch64$"),
}

_ZEILE = re.compile(r"^([A-Za-z0-9_.\-]+)(\[[A-Za-z0-9_,\-]+\])?==([^\s;\\]+)\s*(;[^\\]*)?\\?\s*$")
_HASH = re.compile(r"^\s+--hash=sha256:([0-9a-f]{64})\s*\\?\s*$")


# ── Lock-Datei lesen und schreiben ───────────────────────────────────────────

def eintraege(text: str) -> list[dict]:
    """Anforderungen der Lock-Datei: name, fassung, marke, hashes (in Reihenfolge)."""
    aus: list[dict] = []
    for zeile in text.splitlines():
        if _HASH.match(zeile):
            if not aus:
                raise ValueError(f"--hash ohne Anforderung: {zeile!r}")
            aus[-1]["hashes"].append(_HASH.match(zeile).group(1))
            continue
        roh = zeile.split("#", 1)[0].strip()
        if not roh:
            continue
        m = _ZEILE.match(roh)
        if not m:
            raise ValueError(f"Zeile nicht exakt gepinnt: {zeile!r}")
        aus.append({"name": m.group(1), "extras": m.group(2) or "", "fassung": m.group(3),
                    "marke": (m.group(4) or "").lstrip(";").strip(), "hashes": []})
    return aus


def _anforderung(e: dict) -> str:
    return f"{e['name']}{e['extras']}=={e['fassung']}" + (f"; {e['marke']}" if e["marke"] else "")


def neu_schreiben(text: str, hashes: dict[str, list[str]]) -> str:
    """Kommentare und Reihenfolge behalten, Hash-Zeilen ersetzen."""
    aus: list[str] = []
    for zeile in text.splitlines():
        if _HASH.match(zeile):
            continue
        roh = zeile.split("#", 1)[0].strip()
        if not roh:
            aus.append(zeile)
            continue
        e = eintraege(roh)[0]
        liste = hashes[_norm(e["name"])]
        aus.append(_anforderung(e) + " \\")
        for i, h in enumerate(liste):
            aus.append(f"    --hash=sha256:{h}" + (" \\" if i < len(liste) - 1 else ""))
    return "\n".join(aus) + "\n"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ── Wheel-Namen bewerten ─────────────────────────────────────────────────────

def wheel_tags(dateiname: str) -> list[tuple[str, str, str]] | None:
    """name-fassung(-build)?-py-abi-plattform.whl → alle (py, abi, plattform)."""
    if not dateiname.endswith(".whl"):
        return None
    teile = dateiname[:-4].split("-")
    if len(teile) not in (5, 6):
        return None
    py, abi, plat = teile[-3:]
    return [(p, a, pl) for p in py.split(".") for a in abi.split(".") for pl in plat.split(".")]


def _python_passt(py: str, abi: str, ziel: str) -> bool:
    """Läuft ein Wheel mit Tag (py, abi) auf CPython 3.<ziel>?"""
    if abi == "none" and py in ("py3", f"py3{ziel[1:]}", f"cp{ziel}"):
        return True
    if abi == f"cp{ziel}" and py == f"cp{ziel}":
        return True
    m = re.match(r"^cp3(\d+)$", py)
    if abi == "abi3" and m and int(m.group(1)) <= int(ziel[1:]):
        return True
    return False


def passt(dateiname: str, plattform: str | None = None, python: str | None = None) -> bool:
    """Gehört die Datei zu einer Zielplattform (bzw. genau zu `plattform`/`python`)?"""
    tags = wheel_tags(dateiname)
    if not tags:
        return False
    plattformen = [PLATTFORMEN[plattform]] if plattform else list(PLATTFORMEN.values())
    pythons = [python] if python else list(PYTHON)
    for py, abi, plat in tags:
        if plat != "any" and not any(r.match(plat) for r in plattformen):
            continue
        if any(_python_passt(py, abi, z) for z in pythons):
            return True
    return False


# ── PyPI ─────────────────────────────────────────────────────────────────────

def _pypi(name: str, fassung: str) -> list[dict]:
    url = f"https://pypi.org/pypi/{name}/{fassung}/json"
    with urllib.request.urlopen(url, timeout=60) as r:              # nosec B310 (fest https)
        return json.load(r)["urls"]


def dateien(e: dict, laden=_pypi) -> list[tuple[str, str]]:
    """[(dateiname, sha256)] der passenden Wheels, sortiert nach Dateiname."""
    aus = [(f["filename"], f["digests"]["sha256"]) for f in laden(e["name"], e["fassung"])
           if f.get("packagetype") == "bdist_wheel" and passt(f["filename"])]
    if e["marke"] and "win32" in e["marke"]:
        aus = [(n, h) for n, h in aus if "win_" in n or n.endswith("-any.whl")]
    return sorted(aus)


def soll_hashes(text: str, laden=_pypi) -> dict[str, list[str]]:
    return {_norm(e["name"]): sorted({h for _, h in dateien(e, laden)}) for e in eintraege(text)}


# ── Befehle ──────────────────────────────────────────────────────────────────

def schreiben() -> int:
    text = LOCK.read_text(encoding="utf-8")
    soll = soll_hashes(text)
    leer = [n for n, h in soll.items() if not h]
    if leer:
        print("FEHLER: kein passendes Wheel für " + ", ".join(leer), file=sys.stderr)
        return 1
    LOCK.write_text(neu_schreiben(text, soll), encoding="utf-8")
    print(f"{LOCK.relative_to(WURZEL)}: {sum(len(h) for h in soll.values())} Prüfsummen "
          f"für {len(soll)} Pakete")
    return 0


def pruefen() -> int:
    text = LOCK.read_text(encoding="utf-8")
    soll = soll_hashes(text)
    fehler = 0
    for e in eintraege(text):
        ist, s = sorted(e["hashes"]), soll[_norm(e["name"])]
        if ist != s:
            fehler += 1
            print(f"ABWEICHUNG {e['name']}=={e['fassung']}: Datei {len(ist)}, PyPI {len(s)} "
                  f"(fehlt {len(set(s) - set(ist))}, fremd {len(set(ist) - set(s))})")
    if fehler:
        print("→ python3 tools/lock_hashes.py schreiben", file=sys.stderr)
        return 1
    print(f"OK: Prüfsummen in {LOCK.name} entsprechen PyPI")
    return 0


def abdeckung() -> int:
    """Je Plattform und Python: Pakete ohne passendes Wheel (dort ginge nichts)."""
    text = LOCK.read_text(encoding="utf-8")
    alle = {_norm(e["name"]): (e, [n for n, _ in dateien(e)]) for e in eintraege(text)}
    luecken = 0
    for plat in PLATTFORMEN:
        for py in PYTHON:
            fehlt = []
            for name, (e, namen) in alle.items():
                if e["marke"] and "win32" in e["marke"] and not plat.startswith("win"):
                    continue
                if not any(passt(n, plat, py) for n in namen):
                    fehlt.append(name)
            luecken += len(fehlt)
            print(f"{plat:14} 3.{py[1:]}: " + ("vollständig" if not fehlt else "FEHLT " + ", ".join(fehlt)))
    return 0 if luecken == 0 else 1


def anforderungen(ziel: str, plattform: str) -> int:
    """Lock-Datei mit Prüfsummen, Marken für `plattform` aufgelöst."""
    if plattform not in ("windows", "linux"):
        print("--plattform windows|linux", file=sys.stderr)
        return 2
    zeilen = []
    for e in eintraege(LOCK.read_text(encoding="utf-8")):
        if e["marke"]:
            if e["marke"] != 'sys_platform == "win32"':
                print(f"FEHLER: unbekannte Marke {e['marke']!r}", file=sys.stderr)
                return 1
            if plattform != "windows":
                continue
        zeilen.append(f"{e['name']}{e['extras']}=={e['fassung']} " +
                      " ".join(f"--hash=sha256:{h}" for h in e["hashes"]))
    Path(ziel).write_text("\n".join(zeilen) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["schreiben"]:
        sys.exit(schreiben())
    if a[:1] == ["pruefen"]:
        sys.exit(pruefen())
    if a[:1] == ["abdeckung"]:
        sys.exit(abdeckung())
    if len(a) == 4 and a[0] == "anforderungen" and a[2] == "--plattform":
        sys.exit(anforderungen(a[1], a[3]))
    print(__doc__)
    sys.exit(2)
