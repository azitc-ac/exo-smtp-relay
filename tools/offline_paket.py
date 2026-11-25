#!/usr/bin/env python3
"""offline_paket — Inhalt des Windows-Offline-Pakets zusammenstellen und prüfen.

    python3 tools/offline_paket.py vorbereiten <stufe>
        lädt aus FESTEN Quellen und prüft jede Datei, bevor sie in die Stufe kommt:
          python/   Python-Installer       Adresse + SHA256 aus windows/install.ps1
          module/   ExchangeOnlineManagement-nupkg   (ebenso, $RelayExoModul)
          wheels/   alle Wheels für win_amd64, CPython 3.11–3.13 — `pip download
                    --require-hashes` gegen app/requirements.lock (tools/lock_hashes.py)
          inhalt.json  Fassungen, Dateien und SHA256 von allem oben
    python3 tools/offline_paket.py pruefen <stufe>
        dieselben Prüfungen auf eine fertige Stufe (ohne Netz)

Danach baut `python3 tools/release.py bauen dist <stufe>` daraus
exo-smtp-relay-<version>-windows-offline.zip (steht in SHA256SUMS, wird signiert).

EINE Quelle für Adresse und Prüfsumme: die Tabellen $RelayPythonInstaller und
$RelayExoModul in windows/install.ps1. install.ps1 prüft dieselben Werte beim Kunden
noch einmal — samt Authenticode-Signatur (die kann nur Windows prüfen; das tut der
CI-Job „Offline-Paket“ mit tools/offline_probe.ps1).

WARUM DER VOLLE INSTALLER UND NICHT DIE EMBEDDABLE-DISTRIBUTION
    Die Embeddable-Distribution hat kein venv/ensurepip, keine Registrierung und eine
    ._pth-Datei, die sys.path festschreibt. install.ps1 baut aber eine venv, und der
    Dienst läuft über pythonservice.exe (pywin32), das eine vollständige
    Python-Installation erwartet. Der signierte Installer der PSF ist derselbe Weg wie
    online — nur ohne Download.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "tools"))
import lock_hashes  # noqa: E402

INSTALL = WURZEL / "windows" / "install.ps1"
PYTHON_VERSIONEN = ("3.11", "3.12", "3.13")
PLATTFORM = "win_amd64"


def tabelle(name: str) -> dict[str, str]:
    """`$<name> = @{ Schluessel = "Wert" ... }` aus install.ps1."""
    text = INSTALL.read_text(encoding="utf-8-sig")
    m = re.search(rf"^\${re.escape(name)} = @\{{\s*\n(.*?)^\}}", text, re.M | re.S)
    if not m:
        raise SystemExit(f"FEHLER: ${name} fehlt in {INSTALL}")
    werte = dict(re.findall(r'^\s*(\w+)\s*=\s*"([^"]*)"\s*$', m.group(1), re.M))
    for pflicht in ("Version", "Datei", "Url", "Sha256"):
        if pflicht not in werte:
            raise SystemExit(f"FEHLER: ${name}.{pflicht} fehlt")
    if not re.fullmatch(r"[0-9a-f]{64}", werte["Sha256"]):
        raise SystemExit(f"FEHLER: ${name}.Sha256 ist keine SHA-256 (klein geschrieben)")
    if not werte["Url"].startswith("https://"):
        raise SystemExit(f"FEHLER: ${name}.Url ist nicht https")
    return werte


def sha256(pfad: Path) -> str:
    h = hashlib.sha256()
    with open(pfad, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _laden_geprueft(q: dict[str, str], ziel: Path) -> None:
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_suffix(ziel.suffix + ".tmp")
    with urllib.request.urlopen(q["Url"], timeout=300) as r, open(tmp, "wb") as f:   # nosec B310
        while block := r.read(1 << 20):
            f.write(block)
    ist = sha256(tmp)
    if ist != q["Sha256"]:
        tmp.unlink()
        raise SystemExit(f"FEHLER: {q['Datei']}: Prüfsumme {ist} ≠ {q['Sha256']} — NICHT übernommen")
    tmp.replace(ziel)
    print(f"OK {ziel.relative_to(ziel.parent.parent)}  {ist}")


def _lock_hashes() -> dict[str, set[str]]:
    eintraege = lock_hashes.eintraege((WURZEL / "app" / "requirements.lock").read_text(encoding="utf-8"))
    return {lock_hashes._norm(e["name"]): set(e["hashes"]) for e in eintraege}


def _wheel_paket(datei: str) -> str:
    return lock_hashes._norm(datei.split("-", 1)[0])


def wheels_laden(ziel: Path) -> None:
    ziel.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        anf = Path(tmp) / "windows.txt"
        if lock_hashes.anforderungen(str(anf), "windows") != 0:
            raise SystemExit("FEHLER: Anforderungen für Windows nicht erzeugt")
        for v in PYTHON_VERSIONEN:
            abi = "cp" + v.replace(".", "")
            r = subprocess.run([sys.executable, "-m", "pip", "download", "--quiet",
                                "--disable-pip-version-check", "--require-hashes", "--no-deps",
                                "--only-binary", ":all:", "--platform", PLATTFORM,
                                "--python-version", v, "--implementation", "cp", "--abi", abi,
                                "-d", str(ziel), "-r", str(anf)], capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit(f"FEHLER: pip download für Python {v}: {(r.stderr or r.stdout)[-800:]}")
            print(f"OK Wheels für Python {v} ({PLATTFORM})")


def pruefen(stufe: Path) -> dict:
    """Alles in der Stufe gegen die festen Werte prüfen; liefert inhalt.json-Daten."""
    py, modul = tabelle("RelayPythonInstaller"), tabelle("RelayExoModul")
    for q, teil in ((py, "python"), (modul, "module")):
        pfad = stufe / teil / q["Datei"]
        if not pfad.is_file() or sha256(pfad) != q["Sha256"]:
            raise SystemExit(f"FEHLER: {teil}/{q['Datei']} fehlt oder Prüfsumme falsch")
        fremd = [f.name for f in (stufe / teil).iterdir() if f.name != q["Datei"]]
        if fremd:
            raise SystemExit(f"FEHLER: unerwartete Dateien in {teil}/: {fremd}")
    erlaubt = _lock_hashes()
    wheels = sorted((stufe / "wheels").glob("*"))
    for w in wheels:
        paket = _wheel_paket(w.name)
        if w.suffix != ".whl" or paket not in erlaubt or sha256(w) not in erlaubt[paket]:
            raise SystemExit(f"FEHLER: wheels/{w.name} steht nicht mit dieser Prüfsumme in der Lock-Datei")
    # Jede Python-Fassung hat für jedes Paket ein passendes Wheel.
    for v in PYTHON_VERSIONEN:
        fehlt = [p for p in erlaubt
                 if not any(_wheel_paket(w.name) == p and lock_hashes.passt(w.name, PLATTFORM, v.replace(".", ""))
                            for w in wheels)]
        if fehlt:
            raise SystemExit(f"FEHLER: Python {v}: kein Wheel für {', '.join(fehlt)}")
    return {
        "format": 1,
        "relay": (WURZEL / "VERSION").read_text(encoding="utf-8").strip(),
        "plattform": PLATTFORM,
        "python_versionen": list(PYTHON_VERSIONEN),
        "python": {k.lower(): py[k] for k in ("Version", "Datei", "Url", "Sha256", "Signierer")},
        "modul": {k.lower(): modul[k] for k in ("Name", "Version", "Datei", "Url", "Sha256", "Signierer")},
        "wheels": [{"datei": w.name, "sha256": sha256(w)} for w in wheels],
        "online_bleibt": ["Exchange Online (Postfachabfrage, Connector, Anmeldung) — Dienst von Microsoft"],
    }


def vorbereiten(stufe: Path) -> int:
    if stufe.exists() and any(stufe.iterdir()):
        raise SystemExit(f"FEHLER: {stufe} ist nicht leer")
    py, modul = tabelle("RelayPythonInstaller"), tabelle("RelayExoModul")
    _laden_geprueft(py, stufe / "python" / py["Datei"])
    _laden_geprueft(modul, stufe / "module" / modul["Datei"])
    wheels_laden(stufe / "wheels")
    inhalt = pruefen(stufe)
    (stufe / "inhalt.json").write_text(json.dumps(inhalt, ensure_ascii=False, indent=1) + "\n",
                                       encoding="utf-8")
    groesse = sum(f.stat().st_size for f in stufe.rglob("*") if f.is_file())
    print(f"OK Stufe {stufe}: {len(inhalt['wheels'])} Wheels, {groesse / 1e6:.1f} MB")
    return 0


def pruefen_befehl(stufe: Path) -> int:
    soll = pruefen(stufe)
    ist = json.loads((stufe / "inhalt.json").read_text(encoding="utf-8"))
    if ist != soll:
        raise SystemExit("FEHLER: inhalt.json passt nicht zur Stufe")
    print(f"OK {stufe}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("vorbereiten", "pruefen"):
        print(__doc__)
        sys.exit(2)
    stufe = Path(sys.argv[2])
    sys.exit(vorbereiten(stufe) if sys.argv[1] == "vorbereiten" else pruefen_befehl(stufe))
