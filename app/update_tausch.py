"""Austausch der Anwendung durch ein geprüftes Release — mit Rückfall.

Läuft NICHT im Webprozess, sondern mit den Rechten, die zum Austauschen nötig
sind:

  * Windows: im Dienst-Wrapper `windows/service.py` (SYSTEM). Er hält den
    Anwendungsprozess ohnehin und kann ihn anhalten und neu starten.
  * Linux/systemd: als `exo-smtp-relay-update.service` (root), ausgelöst von
    `exo-smtp-relay-update.path`, sobald `data/update/auftrag.json` erscheint.
    Der Dienst selbst darf `/opt/exo-smtp-relay/app` nicht beschreiben
    (ProtectSystem=strict) — genau so soll es bleiben.
  * Docker: gar nicht. Ein Container tauscht sich nicht selbst aus.

DER EINE ABLAUF (`ablauf`) — für beide Betriebsarten derselbe:

  1. Auftrag lesen und SOFORT löschen (sonst löst die Path-Unit erneut aus).
  2. Phase `pruefen`: Signatur und Prüfsumme ein zweites Mal prüfen (dem
     Webprozess wird nicht vertraut), Paket nach `.update-neu/` entpacken,
     Version kontrollieren. Schlägt das fehl, läuft der Dienst unverändert
     weiter — er wurde noch nicht angehalten.
  3. Dienst anhalten.
  4. Phase `tauschen`: jedes Paketteil (update_paket.PAKET_INHALT) nach
     `.update-alt/` verschieben, das neue an seine Stelle. `data/` und
     `venv/` werden nie angefasst.
  5. Phase `pip`: nur wenn sich `requirements.lock` geändert hat.
  6. Dienst starten und auf das Lebenszeichen der NEUEN Version warten
     (`data/update/lebenszeichen.json`, geschrieben von main.py), danach
     prüfen, ob der Prozess stabil weiterläuft.
  7. Bleibt das aus: anhalten, Phase `zurueck`, alte Fassung starten.

Die Phasen laufen als eigene Prozesse aus einer Arbeitskopie
(`.update-arbeit/`) dieser Datei und von `update_paket.py`: Unter Windows hält
ein Prozess, der `cryptography` geladen hat, dessen DLL fest — pip könnte sie
dann nicht ersetzen. Und während `app/` getauscht wird, soll der Rückfall
nicht aus der neuen, womöglich defekten Fassung laufen.

Diese Datei importiert auf oberster Ebene NUR die Standardbibliothek.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROTOKOLL = 1                  # Fassung der Schnittstelle Webprozess ↔ Tauscher
NEU, ALT, ARBEIT = ".update-neu", ".update-alt", ".update-arbeit"
MARKE = "tausch.json"          # in ALT: was verschoben wurde (für den Rückfall)

WARTE_S = 180                  # so lange auf das Lebenszeichen warten
STABIL_S = 20                  # danach muss der Prozess noch laufen


# ── Dateien im Aktualisierungsbereich data/update/ ───────────────────────────

def bereich(daten: Path) -> Path:
    return Path(daten) / "update"


def auftrag_pfad(daten: Path) -> Path:
    return bereich(daten) / "auftrag.json"


def status_pfad(daten: Path) -> Path:
    return bereich(daten) / "status.json"


def lebenszeichen_pfad(daten: Path) -> Path:
    return bereich(daten) / "lebenszeichen.json"


def json_schreiben(pfad: Path, inhalt: dict) -> None:
    """Atomar (Zwischendatei + os.replace). Kein Geheimnis darin."""
    pfad.parent.mkdir(parents=True, exist_ok=True)
    tmp = pfad.with_name(pfad.name + ".tmp")
    tmp.write_text(json.dumps(inhalt, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, pfad)


def json_lesen(pfad: Path) -> dict:
    try:
        d = json.loads(Path(pfad).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _jetzt() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_setzen(daten: Path, zustand: str, text: str, **mehr) -> None:
    """zustand: wartet | laeuft | ok | fehler | zurueckgerollt."""
    json_schreiben(status_pfad(daten), {"zustand": zustand, "text": text,
                                        "zeit": _jetzt(), **mehr})


def lebenszeichen_schreiben(daten: Path, version: str) -> None:
    """Von main.py nach dem Start des Listeners — der Tauscher erkennt daran,
    dass die NEUE Fassung tatsächlich hochgekommen ist."""
    try:
        json_schreiben(lebenszeichen_pfad(daten),
                       {"version": version, "pid": os.getpid(), "zeit": _jetzt()})
    except OSError:
        pass


def aktuelle_version(wurzel: Path) -> str:
    try:
        return (Path(wurzel) / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def venv_python(wurzel: Path) -> Path:
    w = Path(wurzel) / "venv"
    return w / "Scripts" / "python.exe" if os.name == "nt" else w / "bin" / "python"


# ── Phasen ───────────────────────────────────────────────────────────────────

def _paket_modul():
    """update_paket aus DEMSELBEN Verzeichnis wie diese Datei (Arbeitskopie)."""
    import importlib.util
    pfad = Path(__file__).resolve().parent / "update_paket.py"
    # Schon aus genau dieser Datei geladen (Webprozess, Tests)? Dann dasselbe
    # Modul. Die Phasenprozesse und der Dienst-Wrapper laden es nie vorher.
    geladen = sys.modules.get("update_paket")
    if geladen is not None and Path(getattr(geladen, "__file__", "")).resolve() == pfad:
        return geladen
    spec = importlib.util.spec_from_file_location("_update_paket_tausch", pfad)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def phase_pruefen(wurzel: Path, daten: Path, version: str, paket: str) -> str:
    up = _paket_modul()
    if not up.paket_name_gueltig(paket):
        raise up.PaketFehler(f"Unzulässiger Paketname {paket!r}")
    vk_neu, vk_alt = up.version_schluessel(version), up.version_schluessel(aktuelle_version(wurzel))
    if vk_neu is None:
        raise up.PaketFehler(f"Unzulässige Version {version!r}")
    if vk_alt is not None and vk_neu <= vk_alt:
        raise up.PaketFehler(f"{version} ist nicht neuer als die laufende Fassung.")
    # Erst in ein Verzeichnis kopieren, das nur root/SYSTEM beschreibt, und NUR
    # die Kopie prüfen und entpacken: data/update/ kann auch der Dienst selbst
    # beschreiben — zwischen Prüfen und Entpacken dürfte er sonst tauschen.
    b, pruef = bereich(daten), Path(wurzel) / ARBEIT / "pruef"
    if pruef.exists():
        shutil.rmtree(pruef)
    pruef.mkdir(parents=True)
    for name in (paket, up.SUMMEN, up.SIGNATUR):
        if not (b / name).is_file():
            raise up.PaketFehler(f"{name} fehlt.")
        shutil.copyfile(b / name, pruef / name)
    gefunden = up.paket_pruefen(pruef / paket, (pruef / up.SUMMEN).read_bytes(),
                                (pruef / up.SIGNATUR).read_bytes())
    if gefunden != version:
        raise up.PaketFehler(f"Paket {paket} gehört nicht zu Version {version}.")
    neu = Path(wurzel) / NEU
    if neu.exists():
        shutil.rmtree(neu)
    up.entpacken(pruef / paket, neu, version)
    return f"Signatur und Prüfsumme gültig, {version} entpackt"


def phase_tauschen(wurzel: Path) -> str:
    up = _paket_modul()
    wurzel = Path(wurzel)
    neu, alt = wurzel / NEU, wurzel / ALT
    if not (neu / "app" / "main.py").is_file():
        raise RuntimeError("Kein geprüftes Paket in .update-neu")
    if alt.exists():
        shutil.rmtree(alt)                      # Rückfallstand des VORIGEN Tauschs
    alt.mkdir()
    verschoben, neu_angelegt = [], []
    json_schreiben(alt / MARKE, {"verschoben": verschoben, "neu": neu_angelegt, "fertig": False})
    for teil in up.PAKET_INHALT:
        if not (neu / teil).exists():
            continue
        if (wurzel / teil).exists():
            os.replace(wurzel / teil, alt / teil)
            verschoben.append(teil)
        else:
            neu_angelegt.append(teil)
        json_schreiben(alt / MARKE, {"verschoben": verschoben, "neu": neu_angelegt, "fertig": False})
        os.replace(neu / teil, wurzel / teil)
    json_schreiben(alt / MARKE, {"verschoben": verschoben, "neu": neu_angelegt, "fertig": True})
    shutil.rmtree(neu, ignore_errors=True)
    return f"{len(verschoben) + len(neu_angelegt)} Teile getauscht"


def phase_zurueck(wurzel: Path) -> str:
    wurzel = Path(wurzel)
    alt = wurzel / ALT
    marke = json_lesen(alt / MARKE)
    if not marke:
        return "nichts zurückzunehmen"
    for teil in marke.get("neu", []):
        p = wurzel / teil
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    for teil in marke.get("verschoben", []):
        p = wurzel / teil
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
        os.replace(alt / teil, p)
    shutil.rmtree(alt, ignore_errors=True)
    return f"{len(marke.get('verschoben', []))} Teile zurückgesetzt"


def lock_geaendert(wurzel: Path) -> bool:
    neu = Path(wurzel) / "app" / "requirements.lock"
    alt = Path(wurzel) / ALT / "app" / "requirements.lock"
    try:
        return neu.read_bytes() != alt.read_bytes()
    except OSError:
        return True


def phase_pip(wurzel: Path, lock: Path | None = None) -> str:
    """Abhängigkeiten aus der (neuen bzw. bei `lock` angegebenen) Lock-Datei."""
    python = venv_python(wurzel)
    if not python.is_file():
        return "keine venv — übersprungen"
    lock = lock or Path(wurzel) / "app" / "requirements.lock"
    r = subprocess.run([str(python), "-m", "pip", "install", "--quiet",
                        "--disable-pip-version-check", "-r", str(lock)],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError("pip: " + (r.stderr or r.stdout).strip()[-600:])
    return "Abhängigkeiten angepasst"


def phase_ausfuehren(name: str, wurzel: Path, daten: Path, version: str = "",
                     paket: str = "") -> str:
    if name == "pruefen":
        return phase_pruefen(wurzel, daten, version, paket)
    if name == "tauschen":
        return phase_tauschen(wurzel)
    if name == "pip":
        return phase_pip(wurzel) if lock_geaendert(wurzel) else "Lock-Datei unverändert"
    if name == "zurueck":
        # Abhängigkeiten der alten Fassung zurück, BEVOR deren Dateien
        # zurückwandern (danach liegt die alte Lock-Datei wieder in app/).
        alt_lock = Path(wurzel) / ALT / "app" / "requirements.lock"
        pip_zurueck = alt_lock.is_file() and lock_geaendert(wurzel)
        text = phase_zurueck(wurzel)
        if pip_zurueck:
            text += "; " + phase_pip(wurzel)
        return text
    raise ValueError(f"Unbekannte Phase {name!r}")


# ── Arbeitskopie und Phasen als eigene Prozesse ──────────────────────────────

def arbeitskopie(wurzel: Path) -> Path:
    """update_tausch.py und update_paket.py der LAUFENDEN Fassung nach
    .update-arbeit/ — von dort laufen die Phasen."""
    app = Path(wurzel) / "app"
    ziel = Path(wurzel) / ARBEIT
    if ziel.exists():
        shutil.rmtree(ziel)
    ziel.mkdir()
    for name in ("update_tausch.py", "update_paket.py"):
        shutil.copy2(app / name, ziel / name)
    return ziel / "update_tausch.py"


def phasen_als_prozess(python: Path, skript: Path, wurzel: Path, daten: Path,
                       version: str, paket: str):
    def phase(name: str) -> tuple[bool, str]:
        r = subprocess.run(
            [str(python), str(skript), "phase", name, "--wurzel", str(wurzel),
             "--daten", str(daten), "--version", version, "--paket", paket],
            capture_output=True, text=True, timeout=1200,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ausgabe = (r.stdout.strip().splitlines() or [""])[-1] if r.returncode == 0 \
            else (r.stderr or r.stdout).strip()[-600:]
        return r.returncode == 0, ausgabe
    return phase


def phasen_im_prozess(wurzel: Path, daten: Path, version: str, paket: str):
    """Für Tests: dieselben Phasen ohne eigenen Prozess."""
    def phase(name: str) -> tuple[bool, str]:
        try:
            return True, phase_ausfuehren(name, wurzel, daten, version, paket)
        except Exception as exc:                              # noqa: BLE001
            return False, str(exc)
    return phase


# ── Der Ablauf ───────────────────────────────────────────────────────────────

def gesund_abwarten(daten: Path, version: str, laeuft, warte_s: float = WARTE_S,
                    stabil_s: float = STABIL_S, schlafen=time.sleep, uhr=time.monotonic) -> bool:
    """Lebenszeichen der Zielversion abwarten, dann Stabilität prüfen."""
    ende = uhr() + warte_s
    while uhr() < ende:
        if json_lesen(lebenszeichen_pfad(daten)).get("version") == version and laeuft():
            schlafen(stabil_s)
            return bool(laeuft()) and \
                json_lesen(lebenszeichen_pfad(daten)).get("version") == version
        schlafen(1)
    return False


def ablauf(wurzel: Path, daten: Path, stoppen, starten, laeuft, phase=None,
           warte_s: float = WARTE_S, stabil_s: float = STABIL_S, schlafen=time.sleep,
           uhr=time.monotonic) -> str:
    """Ein Auftrag von Anfang bis Ende. Rückgabe: Endzustand (wie status.json).

    `phase` None = Phasen als eigene Prozesse aus der Arbeitskopie (Betrieb);
    Tests übergeben `phasen_im_prozess(...)`."""
    wurzel, daten = Path(wurzel), Path(daten)
    auftrag = json_lesen(auftrag_pfad(daten))
    try:
        auftrag_pfad(daten).unlink()
    except OSError:
        pass
    version, paket = str(auftrag.get("version") or ""), str(auftrag.get("paket") or "")
    von = aktuelle_version(wurzel)
    kopf = {"von": von, "nach": version, "angefordert_von": auftrag.get("angefordert_von", "")}
    if not version or not paket:
        status_setzen(daten, "fehler", "Auftrag unvollständig", **kopf)
        return "fehler"
    if phase is None:
        skript = arbeitskopie(wurzel)
        python = venv_python(wurzel)
        phase = phasen_als_prozess(python if python.is_file() else Path(sys.executable),
                                   skript, wurzel, daten, version, paket)

    status_setzen(daten, "laeuft", "Paket wird geprüft", **kopf)
    ok, text = phase("pruefen")
    if not ok:
        shutil.rmtree(wurzel / NEU, ignore_errors=True)
        shutil.rmtree(wurzel / ARBEIT, ignore_errors=True)
        status_setzen(daten, "fehler", f"Nicht eingespielt: {text}", **kopf)
        return "fehler"

    status_setzen(daten, "laeuft", "Dienst wird angehalten und getauscht", **kopf)
    stoppen()
    for name in ("tauschen", "pip"):
        ok, text = phase(name)
        if not ok:
            _, zurueck = phase("zurueck")
            starten()
            status_setzen(daten, "zurueckgerollt",
                          f"Phase {name} fehlgeschlagen: {text} — {zurueck}; {von} läuft weiter",
                          **kopf)
            return "zurueckgerollt"

    try:
        lebenszeichen_pfad(daten).unlink()
    except OSError:
        pass
    status_setzen(daten, "laeuft", f"{version} startet", **kopf)
    starten()
    if gesund_abwarten(daten, version, laeuft, warte_s, stabil_s, schlafen, uhr):
        shutil.rmtree(wurzel / ARBEIT, ignore_errors=True)
        for name in ("SHA256SUMS", "SHA256SUMS.sig", paket):
            try:
                (bereich(daten) / name).unlink()
            except OSError:
                pass
        status_setzen(daten, "ok", f"{version} läuft (vorher {von})", **kopf)
        return "ok"

    stoppen()
    _, zurueck = phase("zurueck")
    starten()
    status_setzen(daten, "zurueckgerollt",
                  f"{version} kam nicht stabil hoch — {zurueck}; {von} läuft wieder", **kopf)
    return "zurueckgerollt"


# ── systemd ──────────────────────────────────────────────────────────────────

def _systemctl(*args: str) -> int:
    return subprocess.run(["systemctl", *args], capture_output=True, timeout=120).returncode


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Austausch des EXO SMTP Relay")
    p.add_argument("befehl", choices=("ablauf", "phase"))
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--wurzel", required=True)
    p.add_argument("--daten", default="")
    p.add_argument("--version", default="")
    p.add_argument("--paket", default="")
    p.add_argument("--dienst", default="exo-smtp-relay")
    a = p.parse_args(argv)
    wurzel = Path(a.wurzel)
    daten = Path(a.daten) if a.daten else wurzel / "data"
    if a.befehl == "phase":
        try:
            print(phase_ausfuehren(a.name, wurzel, daten, a.version, a.paket))
            return 0
        except Exception as exc:                              # noqa: BLE001
            print(str(exc), file=sys.stderr)
            return 1
    # ablauf — systemd (Windows ruft `ablauf()` direkt aus service.py)
    if not auftrag_pfad(daten).exists():
        return 0
    ende = ablauf(wurzel, daten,
                  stoppen=lambda: _systemctl("stop", a.dienst),
                  starten=lambda: _systemctl("start", a.dienst),
                  laeuft=lambda: _systemctl("is-active", "--quiet", a.dienst) == 0)
    print(ende)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
