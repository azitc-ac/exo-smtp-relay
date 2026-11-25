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
import uuid
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


def ruecknahme_offen_pfad(daten: Path) -> Path:
    """Liegt hier, wenn eine Rücknahme scheiterte (Sperre): Lauf-ID, von, nach. Beim nächsten
    Dienststart bzw. Auftrag wird sie erneut versucht (ruecknahme_nachholen)."""
    return bereich(daten) / "ruecknahme_offen.json"


# Wiederholen beim Schreiben (Labtest 0.2.54, Windows): Ein fremder Leser ohne FileShare.Delete
# (Virenscanner, Sicherung) lässt os.replace mit WinError 5 scheitern — bisher brach das ablauf()
# ab, die neue Fassung lief ohne Überwachung. Kurz wiederholen, mit wachsender Pause.
SCHREIB_PAUSEN_S: tuple[float, ...] = (0.1, 0.2, 0.4, 0.8, 1.5, 2.0, 2.5, 2.5)     # ≈ 10 s


def json_schreiben(pfad: Path, inhalt: dict) -> None:
    """Atomar (Zwischendatei + os.replace), UTF-8 ohne BOM. Wiederholt bei OSError (Sperre durch
    einen fremden Leser); scheitert es endgültig, kommt die letzte Ausnahme. Kein Geheimnis darin."""
    pfad = Path(pfad)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    tmp = pfad.with_name(pfad.name + ".tmp")
    daten = json.dumps(inhalt, ensure_ascii=False, indent=1)
    for i, pause in enumerate((0.0,) + SCHREIB_PAUSEN_S):
        if pause:
            time.sleep(pause)
        try:
            tmp.write_text(daten, encoding="utf-8")
            os.replace(tmp, pfad)
            return
        except OSError:
            if i == len(SCHREIB_PAUSEN_S):
                raise


class JsonUnlesbar(ValueError):
    """Die Datei gibt es, sie ist aber kein lesbares JSON-Objekt — NICHT dasselbe wie „fehlt“."""


def json_lesen_streng(pfad: Path) -> dict | None:
    """None, wenn es die Datei nicht gibt; sonst das Objekt (UTF-8, BOM erlaubt). Kaputt oder
    unlesbar → JsonUnlesbar (Labtest 0.2.54: install.ps1 schrieb mit BOM, json_lesen lieferte {},
    und „{}“ galt als „nichts offen“)."""
    pfad = Path(pfad)
    if not pfad.exists():
        return None
    try:
        d = json.loads(pfad.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise JsonUnlesbar(f"{pfad.name} unlesbar: {exc}") from exc
    if not isinstance(d, dict):
        raise JsonUnlesbar(f"{pfad.name} ist kein JSON-Objekt")
    return d


def json_lesen(pfad: Path) -> dict:
    """Nachsichtig: {} bei fehlender oder kaputter Datei. Nur, wo {} die SICHERE Bedeutung hat
    (nicht bereit, nicht gesund, kein Auftrag); sonst json_lesen_streng."""
    try:
        return json_lesen_streng(pfad) or {}
    except JsonUnlesbar:
        return {}


def _jetzt() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_setzen(daten: Path, zustand: str, text: str, **mehr) -> None:
    """zustand: wartet | laeuft | ok | fehler | zurueckgerollt. Bricht NIE ab: Der Status ist
    Anzeige — scheitert er endgültig, steht es auf stderr (Dienstprotokoll), der Ablauf geht
    weiter (Labtest 0.2.54: eine gesperrte status.json beendete ablauf())."""
    try:
        json_schreiben(status_pfad(daten), {"zustand": zustand, "text": text,
                                            "zeit": _jetzt(), **mehr})
    except OSError as exc:
        print(f"update_tausch: Status nicht geschrieben ({zustand}: {text[:120]}): {exc}", file=sys.stderr)


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


def _beiseite(pfad: Path) -> Path:
    """Freier Name neben `pfad` (…​.relay-verworfen[-N])."""
    ziel, i = pfad.with_name(pfad.name + ".relay-verworfen"), 1
    while ziel.exists():
        ziel, i = pfad.with_name(f"{pfad.name}.relay-verworfen-{i}"), i + 1
    return ziel


def _weg(pfad: Path) -> None:
    """Müll entfernen, so weit es geht — ein Rest stört nicht (nie „erst löschen, dann ersetzen“)."""
    if pfad.is_dir():
        shutil.rmtree(pfad, ignore_errors=True)
    else:
        try:
            pfad.unlink()
        except OSError:
            pass


# ── Windows: was sonst nur install.ps1 setzt ─────────────────────────────────
WINDOWS = os.name == "nt"
WRAPPER_NEUSTART = "wrapper_neustart.json"     # in data/update: erwartete Wrapper-Fassung
NEUSTART_AUFGABE = "ExoSmtpRelay-Neustart"


def _powershell(args: list[str]) -> tuple[int, str]:
    umgebung = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                       env=umgebung, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def daten_rechte_setzen(wurzel: Path) -> str:
    """Rechte auf data\\ wie bei der Installation erzwingen — über DIESELBE Funktion:
    windows\\install.ps1 -NurDatenRechte (Set-RelayDataAcl). Wer nur per Knopf aktualisierte,
    bekam die Rechte aus 0.2.45 sonst nie. Scheitern bricht nichts ab, steht aber im Status."""
    if not WINDOWS:
        return ""
    skript = Path(wurzel) / "windows" / "install.ps1"
    try:
        rc, aus = _powershell(["-File", str(skript), "-NurDatenRechte", "-InstallDir", str(wurzel)])
    except Exception as exc:                                  # noqa: BLE001
        return f"WARNUNG: Rechte auf data\\ nicht geprüft ({exc})"
    zeilen = [z.strip() for z in aus.splitlines() if z.strip()]
    entfernt = [z[4:] for z in zeilen if z.startswith("OK: entfernt") or z.startswith("OK: Vererbung wieder an")]
    warnungen = [z[9:] for z in zeilen if z.startswith("WARNUNG: ")]
    if rc != 0:
        return f"WARNUNG: Rechte auf data\\ nicht gesetzt (Exit {rc}): {' | '.join(zeilen[-3:])}"
    teile = ["Rechte auf data\\: " + ("; ".join(entfernt) if entfernt else "unverändert")]
    if warnungen:
        teile.append("WARNUNG: " + " | ".join(warnungen))
    return "; ".join(teile)


def wrapper_fassung_datei(wurzel: Path) -> str:
    import hashlib
    f = Path(wurzel) / "windows" / "service.py"
    return hashlib.sha256(f.read_bytes()).hexdigest()[:12] if f.is_file() else ""


def _neustart_einplanen() -> None:
    """Neustart des Windows-Dienstes NICHT aus dem Dienst heraus (er beendete sich mitten im Lauf),
    sondern per einmaliger Aufgabe als SYSTEM, die sich danach selbst löscht."""
    befehl = (f'powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep 15; '
              f'Restart-Service ExoSmtpRelay -Force; schtasks /Delete /TN {NEUSTART_AUFGABE} /F"')
    for args in (["schtasks", "/Create", "/TN", NEUSTART_AUFGABE, "/SC", "ONCE", "/ST", "00:00",
                  "/RU", "SYSTEM", "/RL", "HIGHEST", "/TR", befehl, "/F"],
                 ["schtasks", "/Run", "/TN", NEUSTART_AUFGABE]):
        r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[:200]}")


def wrapper_abgleichen(wurzel: Path, daten: Path) -> str:
    """Windows: Weicht windows\\service.py auf der Platte von der LAUFENDEN Wrapper-Fassung ab
    (waechter.json, beim Start geschrieben), Neustart des Windows-Dienstes einplanen — nach einem
    Tausch auf die neue, nach einer Rücknahme auf die alte Fassung. Der Wrapper bestätigt beim
    Start (wrapper_gestartet)."""
    if not WINDOWS:
        return ""
    laufend = str(json_lesen(bereich(daten) / "waechter.json").get("fassung") or "")
    datei = wrapper_fassung_datei(wurzel)
    if not datei or datei == laufend:
        return ""
    try:
        json_schreiben(bereich(daten) / WRAPPER_NEUSTART, {"erwartet": datei, "vorher": laufend, "zeit": _jetzt()})
        _neustart_einplanen()
    except Exception as exc:                                  # noqa: BLE001
        return (f"WARNUNG: Dienst-Wrapper {laufend or 'alt'} -> {datei}, Neustart NICHT eingeplant ({exc}) — "
                "von Hand: Restart-Service ExoSmtpRelay")
    return f"Dienst-Wrapper {laufend or 'alt'} -> {datei}: Neustart des Windows-Dienstes eingeplant"


def wrapper_gestartet(daten: Path, fassung: str) -> None:
    """Vom Wrapper beim Start (nach waechter.json): bestätigt einen eingeplanten Neustart im Status."""
    pfad = bereich(daten) / WRAPPER_NEUSTART
    erwartet = json_lesen(pfad)
    if not erwartet:
        return
    try:
        pfad.unlink()
    except OSError:
        pass
    s = json_lesen(status_pfad(daten))
    if fassung == erwartet.get("erwartet"):
        zusatz = f"Dienst-Wrapper neu gestartet (Fassung {fassung})"
    else:
        zusatz = (f"WARNUNG: Dienst-Wrapper läuft in Fassung {fassung}, erwartet {erwartet.get('erwartet')} — "
                  "Restart-Service ExoSmtpRelay")
    status_setzen(daten, str(s.get("zustand") or "ok"), (str(s.get("text") or "") + "; " + zusatz).lstrip("; "),
                  **{k: s[k] for k in ("von", "nach", "angefordert_von") if k in s})


# ── systemd-Units mitziehen ──────────────────────────────────────────────────
# Wer per Knopf aktualisiert, bekäme sonst nie eine neue Unit (0.2.55: ExecStartPre für das
# Nachholen einer offenen Rücknahme). Der Tauscher läuft unter systemd als root.
SYSTEMD_ZIEL = Path("/etc/systemd/system")
EINHEITEN = ("exo-smtp-relay.service", "exo-smtp-relay-update.service", "exo-smtp-relay-update.path")
EINHEITEN_ALT = "einheiten"                    # in .update-alt: die ersetzten Units


def systemd_ziel() -> Path | None:
    """/etc/systemd/system, wenn das Relay dort als Unit installiert ist (nicht Windows/Docker)."""
    if os.name == "nt" or not (SYSTEMD_ZIEL / EINHEITEN[0]).is_file():
        return None
    return SYSTEMD_ZIEL


def systemd_neu_laden() -> None:
    if _systemctl("daemon-reload") != 0:
        raise RuntimeError("systemctl daemon-reload fehlgeschlagen")


def _datei_ersetzen(ziel: Path, inhalt: bytes) -> None:
    tmp = ziel.with_name(ziel.name + ".relay-neu")
    tmp.write_bytes(inhalt)
    os.chmod(tmp, 0o644)
    os.replace(tmp, ziel)


def einheiten_tauschen(wurzel: Path, alt: Path, merker: dict) -> str:
    """Units aus dem (schon getauschten) linux/ einspielen, die alten nach .update-alt/einheiten.
    Nur Units, die einer BEKANNTEN Fassung entsprechen (linux/einheiten-bekannt.json); vom
    Betreiber angepasste bleiben und werden gemeldet. Drop-ins (*.service.d) fasst das nie an.
    Merker vor jedem Ersetzen fortgeschrieben (dieselbe Lauf-ID wie der Tausch)."""
    import hashlib
    ziel = systemd_ziel()
    if ziel is None:
        return ""
    bekannt = json_lesen_streng(Path(wurzel) / "linux" / "einheiten-bekannt.json") or {}
    getauscht, angepasst = [], []
    for name in EINHEITEN:
        neu, inst = Path(wurzel) / "linux" / name, ziel / name
        if not neu.is_file() or not inst.is_file():
            continue
        vorhanden = inst.read_bytes()
        if vorhanden == neu.read_bytes():
            continue
        if hashlib.sha256(vorhanden).hexdigest() not in bekannt.get(name, []):
            angepasst.append(name)
            continue
        (alt / EINHEITEN_ALT).mkdir(exist_ok=True)
        (alt / EINHEITEN_ALT / name).write_bytes(vorhanden)
        merker.setdefault("einheiten", []).append(name)
        json_schreiben(alt / MARKE, merker)
        _datei_ersetzen(inst, neu.read_bytes())
        getauscht.append(name)
    if getauscht:
        systemd_neu_laden()
    teile = []
    if getauscht:
        teile.append("Units aktualisiert: " + ", ".join(getauscht))
    if angepasst:
        teile.append("Units vom Betreiber angepasst, NICHT getauscht: " + ", ".join(angepasst)
                     + " (neue Fassung unter linux/ — von Hand abgleichen)")
    return "; ".join(teile)


def einheiten_zurueck(alt: Path, marke: dict) -> None:
    """Gegenstück: die gesicherten Units zurück, daemon-reload. Wiederholbar."""
    ziel = systemd_ziel() if marke.get("einheiten") else None
    zurueck = False
    for name in marke.get("einheiten", []):
        gesichert = alt / EINHEITEN_ALT / name
        if ziel is None or not gesichert.is_file():
            continue
        _datei_ersetzen(ziel / name, gesichert.read_bytes())
        gesichert.unlink()
        zurueck = True
    if zurueck:
        systemd_neu_laden()


def phase_tauschen(wurzel: Path, version: str = "", lauf: str = "") -> str:
    """Verschoben wird nur mit os.replace (MoveFileExW unter Windows: ein Rename, scheitert ganz
    oder gar nicht — anders als PowerShells Move-Item, Labtest 0.2.50; belegt in
    tests/test_aktualisierung.py::test_os_replace_ordner_mit_offener_datei_atomar)."""
    up = _paket_modul()
    wurzel = Path(wurzel)
    neu, alt = wurzel / NEU, wurzel / ALT
    if not (neu / "app" / "main.py").is_file():
        raise RuntimeError("Kein geprüftes Paket in .update-neu")
    if alt.exists():
        # Rückfallstand des VORIGEN Tauschs: erst atomar beiseite (scheitert das, ist nichts
        # verändert), dann entfernen, so weit es geht.
        weg = _beiseite(alt)
        os.replace(alt, weg)
        _weg(weg)
    alt.mkdir()
    verschoben, neu_angelegt = [], []
    json_schreiben(alt / MARKE, {"verschoben": verschoben, "neu": neu_angelegt, "fertig": False,
                                 "nach": version, "lauf": lauf})
    for teil in up.PAKET_INHALT:
        if not (neu / teil).exists():
            continue
        if (wurzel / teil).exists():
            os.replace(wurzel / teil, alt / teil)
            verschoben.append(teil)
        else:
            neu_angelegt.append(teil)
        json_schreiben(alt / MARKE, {"verschoben": verschoben, "neu": neu_angelegt, "fertig": False,
                                     "nach": version, "lauf": lauf})
        os.replace(neu / teil, wurzel / teil)
    merker = {"verschoben": verschoben, "neu": neu_angelegt, "fertig": False, "nach": version, "lauf": lauf}
    einheiten = einheiten_tauschen(wurzel, alt, merker)
    merker["fertig"] = True
    json_schreiben(alt / MARKE, merker)
    shutil.rmtree(neu, ignore_errors=True)
    return f"{len(verschoben) + len(neu_angelegt)} Teile getauscht" + (f"; {einheiten}" if einheiten else "")


def marke_gehoert_zu(wurzel: Path, lauf: str) -> bool:
    """Gehört der Rückfallstand in .update-alt zu DIESEM Tausch? Nur bei gleicher Lauf-ID —
    fehlt sie (Updater ≤ 0.2.53, auch 0.2.42) oder ist sie fremd, NIE (Labtest 0.2.53 l6: das
    Beiseitelegen des alten .update-alt scheiterte, bevor der neue Merker geschrieben war; die
    Rücknahme nahm den ALTEN Merker und holte app\\ aus der vorvorigen Fassung zurück)."""
    marke = json_lesen_streng(Path(wurzel) / ALT / MARKE) or {}
    return bool(lauf) and marke.get("lauf") == lauf


def phase_zurueck(wurzel: Path, lauf: str = "", alt_entfernen: bool = True) -> str:
    """Neues beiseite (atomar), Altes zurück (atomar), dann Müll weg — nie „erst löschen, dann
    ersetzen“: ein gesperrter neuer Teil würde sonst halb gelöscht (Labtest 0.2.50).
    Nur der Rückfallstand mit der Lauf-ID DIESES Tauschs zählt. Wiederholbar: Teile, die schon
    zurück sind (nicht mehr in .update-alt), werden übersprungen — scheitert ein Teil, bleibt
    .update-alt samt Marke für den nächsten Versuch liegen."""
    wurzel = Path(wurzel)
    alt = wurzel / ALT
    marke = json_lesen_streng(alt / MARKE)        # kaputt → Ausnahme, NICHT „nichts zurückzunehmen“
    if not marke:
        return "nichts zurückzunehmen"
    if not marke_gehoert_zu(wurzel, lauf):
        return (f"Rückfallstand gehört nicht zu diesem Tausch (Lauf {marke.get('lauf') or 'ohne Kennung'})"
                " — nichts zurückgenommen, nichts verändert")
    einheiten_zurueck(alt, marke)
    muell = []
    for teil in marke.get("neu", []):
        p = wurzel / teil
        if p.exists():
            w = _beiseite(p)
            os.replace(p, w)
            muell.append(w)
    for teil in marke.get("verschoben", []):
        p, a = wurzel / teil, alt / teil
        if not a.exists():
            continue                                # schon zurück (früherer Versuch)
        if p.exists():
            w = _beiseite(p)
            os.replace(p, w)
            muell.append(w)
        try:
            os.replace(a, p)
        except OSError:
            if not p.exists() and muell and muell[-1].name.startswith(p.name + ".relay-verworfen"):
                os.replace(muell.pop(), p)          # lieber die neue Fassung als keine
            raise
    for w in muell:
        _weg(w)
    if alt_entfernen:
        shutil.rmtree(alt, ignore_errors=True)
    return f"{len(marke.get('verschoben', []))} Teile zurückgesetzt"


def reste_aufraeumen(wurzel: Path) -> list[str]:
    """*.relay-verworfen* neben Teilen, die am Platz sind, entfernen (Müll gescheiterter
    Rücknahmen). Was sich nicht löschen lässt, bleibt und wird genannt."""
    up = _paket_modul()
    wurzel = Path(wurzel)
    bleibt = []
    for teil in up.PAKET_INHALT:
        if not (wurzel / teil).exists():
            continue
        for rest in wurzel.glob(f"{teil}.relay-verworfen*"):
            _weg(rest)
            if rest.exists():
                bleibt.append(rest.name)
    return bleibt


def lock_geaendert(wurzel: Path) -> bool:
    neu = Path(wurzel) / "app" / "requirements.lock"
    alt = Path(wurzel) / ALT / "app" / "requirements.lock"
    try:
        return neu.read_bytes() != alt.read_bytes()
    except OSError:
        return True


OFFLINE = "offline"             # Offline-Installation: offline/inhalt.json + offline/wheels/


def offline_wheels(wurzel: Path) -> Path | None:
    """Wheels einer Offline-Installation (install.ps1 aus dem Offline-Paket), sonst None."""
    w = Path(wurzel) / OFFLINE
    return w / "wheels" if (w / "inhalt.json").is_file() and (w / "wheels").is_dir() else None


def lock_hat_pruefsummen(lock: Path) -> bool:
    try:
        return "--hash=sha256:" in Path(lock).read_text(encoding="utf-8")
    except OSError:
        return True                     # im Zweifel streng


def pip_argumente(wurzel: Path, lock: Path) -> list[str]:
    """`pip install`-Argumente — DIESELBEN wie in install.ps1: nur Wheels, nur mit den
    Prüfsummen aus der Lock-Datei; offline zusätzlich ohne Index, nur aus offline/wheels."""
    args = ["-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    # Eine Lock-Datei OHNE Prüfsummen gibt es nur in Fassungen vor 0.2.47 — beim Rückfall auf
    # eine solche installiert pip sie so, wie sie damals installiert wurde (Labtest 0.2.48, e6a:
    # sonst „Hashes are required“, und die neuen Pakete blieben). Jede Lock ab 0.2.47 hat sie.
    if lock_hat_pruefsummen(lock):
        args += ["--require-hashes", "--only-binary", ":all:"]
    wheels = offline_wheels(wurzel)
    if wheels is not None:
        args += ["--no-index", "--find-links", str(wheels)]
    return args + ["-r", str(lock)]


def phase_pip(wurzel: Path, lock: Path | None = None) -> str:
    """Abhängigkeiten aus der (neuen bzw. bei `lock` angegebenen) Lock-Datei."""
    python = venv_python(wurzel)
    if not python.is_file():
        return "keine venv — übersprungen"
    lock = lock or Path(wurzel) / "app" / "requirements.lock"
    r = subprocess.run([str(python), *pip_argumente(wurzel, lock)],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError("pip: " + (r.stderr or r.stdout).strip()[-600:])
    if offline_wheels(wurzel) is not None:
        return "Abhängigkeiten angepasst (offline, mitgelieferte Wheels)"
    return "Abhängigkeiten angepasst"


def phase_ausfuehren(name: str, wurzel: Path, daten: Path, version: str = "",
                     paket: str = "", lauf: str = "") -> str:
    if name == "pruefen":
        return phase_pruefen(wurzel, daten, version, paket)
    if name == "tauschen":
        return phase_tauschen(wurzel, version, lauf)
    if name == "pip":
        return phase_pip(wurzel) if lock_geaendert(wurzel) else "Lock-Datei unverändert"
    if name == "zurueck":
        # Abhängigkeiten der alten Fassung zurück, BEVOR deren Dateien
        # zurückwandern (danach liegt die alte Lock-Datei wieder in app/).
        if not marke_gehoert_zu(wurzel, lauf):
            return phase_zurueck(wurzel, lauf)      # meldet „nichts zurückgenommen“, kein pip
        # Ob pip nötig ist, steht im Merker — ein zweiter Versuch (Sperre beim ersten) weiss es
        # sonst nicht mehr, weil app/ schon zurück ist. .update-alt verschwindet erst NACH pip:
        # scheitert pip, bleibt der Merker für den nächsten Versuch (bisher war er weg und ein
        # Nachholen meldete Erfolg ohne Abhängigkeiten).
        alt = Path(wurzel) / ALT
        marke = json_lesen_streng(alt / MARKE) or {}
        if "pip_noetig" not in marke:
            alt_lock = alt / "app" / "requirements.lock"
            marke["pip_noetig"] = bool(alt_lock.is_file() and lock_geaendert(wurzel))
            json_schreiben(alt / MARKE, marke)
        text = phase_zurueck(wurzel, lauf, alt_entfernen=False)
        if marke["pip_noetig"]:
            text += "; " + phase_pip(wurzel)
        shutil.rmtree(alt, ignore_errors=True)
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
    def phase(name: str, lauf: str = "") -> tuple[bool, str]:
        r = subprocess.run(
            [str(python), str(skript), "phase", name, "--wurzel", str(wurzel),
             "--daten", str(daten), "--version", version, "--paket", paket, "--lauf", lauf],
            capture_output=True, text=True, timeout=1200,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ausgabe = (r.stdout.strip().splitlines() or [""])[-1] if r.returncode == 0 \
            else (r.stderr or r.stdout).strip()[-600:]
        return r.returncode == 0, ausgabe
    return phase


def phasen_im_prozess(wurzel: Path, daten: Path, version: str, paket: str):
    """Für Tests: dieselben Phasen ohne eigenen Prozess."""
    def phase(name: str, lauf: str = "") -> tuple[bool, str]:
        try:
            return True, phase_ausfuehren(name, wurzel, daten, version, paket, lauf)
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
    if auftrag.get("nachholen"):
        # Knopf bei offener Rücknahme (aktualisierung.einspielen): nur nachholen.
        stoppen()
        nachgeholt = ruecknahme_nachholen(wurzel, daten)
        starten()
        return "zurueckgerollt" if nachgeholt else "fehler"
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

    # Eine Rücknahme von früher ist noch offen (Sperre)? Erst die — sonst kein neuer Tausch.
    if ruecknahme_offen_pfad(daten).exists():
        stoppen()
        nachgeholt = ruecknahme_nachholen(wurzel, daten, phase)
        starten()
        if not nachgeholt:
            status_setzen(daten, "fehler", "Nicht eingespielt: eine frühere Rücknahme ist noch offen "
                          f"und gelang wieder nicht — {_offen_hinweis(wurzel)}", **kopf)
            return "fehler"

    lauf = uuid.uuid4().hex                     # kennzeichnet .update-alt dieses Tauschs
    status_setzen(daten, "laeuft", "Paket wird geprüft", **kopf)
    ok, text = phase("pruefen", lauf)
    if not ok:
        shutil.rmtree(wurzel / NEU, ignore_errors=True)
        shutil.rmtree(wurzel / ARBEIT, ignore_errors=True)
        status_setzen(daten, "fehler", f"Nicht eingespielt: {text}", **kopf)
        return "fehler"

    status_setzen(daten, "laeuft", "Dienst wird angehalten und getauscht", **kopf)
    stoppen()
    hinweise = []
    for name in ("tauschen", "pip"):
        ok, text = phase(name, lauf)
        if ok and name == "tauschen" and "Units" in text:
            hinweise.append(text.split("; ", 1)[1] if "; " in text else text)
        if ok and name == "pip":
            r = daten_rechte_setzen(wurzel)
            if r:
                hinweise.append(r)
        if not ok:
            return _zuruecknehmen(wurzel, daten, phase, lauf, starten, kopf,
                                  f"Phase {name} fehlgeschlagen: {text}")

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
        bleibt = reste_aufraeumen(wurzel)
        w = wrapper_abgleichen(wurzel, daten)
        if w:
            hinweise.append(w)
        status_setzen(daten, "ok", f"{version} läuft (vorher {von})"
                      + "".join(f"; {h}" for h in hinweise)
                      + (f"; nicht löschbare Reste: {', '.join(bleibt)}" if bleibt else ""), **kopf)
        return "ok"

    stoppen()
    return _zuruecknehmen(wurzel, daten, phase, lauf, starten, kopf,
                          f"{version} kam nicht stabil hoch")


def _offen_hinweis(wurzel: Path) -> str:
    """Plattformrichtig (Labtest 0.2.54: unter Linux stand dort windows\\install.ps1)."""
    if os.name == "nt":
        befehl = ("Sperre lösen und den Dienst neu starten (Restart-Service ExoSmtpRelay) — die Rücknahme "
                  "wird beim Start nachgeholt — oder windows\\install.ps1 aus dem Paket der vorigen Fassung")
    else:
        befehl = ("Sperre lösen und sudo systemctl restart exo-smtp-relay — die Rücknahme wird beim Start "
                  "nachgeholt (ExecStartPre)")
    return (f"am Platz: Fassung {aktuelle_version(wurzel) or '?'}; {befehl}, oder auf der Seite "
            "Aktualisierung „Rücknahme nachholen“. Der nächste Dienststart spielt die vorige Fassung "
            "zurück — auch wenn die neue inzwischen läuft")


def _zuruecknehmen(wurzel: Path, daten: Path, phase, lauf: str, starten, kopf: dict, grund: str) -> str:
    """Rücknahme mit AUSGEWERTETEM Ergebnis (Labtest 0.2.53 l4b: ein Fehlschlag wurde verworfen,
    der Status meldete „läuft wieder“, obwohl die neue VERSION am Platz lag und nichts lauschte)."""
    ok, zurueck = phase("zurueck", lauf)
    if ok:
        starten()
        w = wrapper_abgleichen(wurzel, daten)
        if w:
            zurueck += f"; {w}"
        try:
            ruecknahme_offen_pfad(daten).unlink()
        except OSError:
            pass
        status_setzen(daten, "zurueckgerollt",
                      f"{grund} — {zurueck}; {aktuelle_version(wurzel)} läuft wieder", **kopf)
        return "zurueckgerollt"
    try:
        json_schreiben(ruecknahme_offen_pfad(daten), {"lauf": lauf, "zeit": _jetzt(), "grund": zurueck, **kopf})
    except OSError as exc:
        zurueck += f"; Merker ruecknahme_offen.json NICHT geschrieben ({exc}) — Nachholen nur von Hand"
    starten()
    status_setzen(daten, "fehler", f"{grund} — Rücknahme UNVOLLSTÄNDIG: {zurueck}; {_offen_hinweis(wurzel)}",
                  **kopf)
    return "fehler"


def ruecknahme_nachholen(wurzel: Path, daten: Path, phase=None) -> bool:
    """Offene Rücknahme (ruecknahme_offen.json) erneut versuchen. True NUR, wenn nichts offen ist
    oder die Rücknahme jetzt gelang. Eine unlesbare Datei ist NICHT „nichts offen“ (Labtest
    0.2.54: mit BOM geschrieben → {} → „nachgeholt“, SMTP blieb aus)."""
    wurzel, daten = Path(wurzel), Path(daten)
    try:
        offen = json_lesen_streng(ruecknahme_offen_pfad(daten))
    except JsonUnlesbar as exc:
        status_setzen(daten, "fehler", f"Offene Rücknahme NICHT nachgeholt: {exc}; {_offen_hinweis(wurzel)}")
        return False
    if offen is None:
        return True
    lauf = str(offen.get("lauf") or "")
    kopf = {k: offen.get(k, "") for k in ("von", "nach", "angefordert_von")}
    if not lauf:
        status_setzen(daten, "fehler", f"Offene Rücknahme NICHT nachgeholt: ohne Lauf-Kennung; {_offen_hinweis(wurzel)}",
                      **kopf)
        return False
    if phase is None:
        phase = phasen_im_prozess(wurzel, daten, str(offen.get("nach") or ""), "")
    ok, text = phase("zurueck", lauf)
    if not ok:
        status_setzen(daten, "fehler", f"Rücknahme weiter UNVOLLSTÄNDIG: {text}; {_offen_hinweis(wurzel)}", **kopf)
        return False
    try:
        ruecknahme_offen_pfad(daten).unlink()
    except OSError:
        pass
    bleibt = reste_aufraeumen(wurzel)
    w = wrapper_abgleichen(wurzel, daten)
    if w:
        text += f"; {w}"
    status_setzen(daten, "zurueckgerollt", f"Rücknahme nachgeholt: {text}; {aktuelle_version(wurzel)} läuft wieder"
                  + (f"; nicht löschbare Reste: {', '.join(bleibt)}" if bleibt else ""), **kopf)
    return True


def beim_dienststart(wurzel: Path, daten: Path, wrapper_fassung: str = "") -> bool | None:
    """DER gemeinsame Pfad beim Dienststart, bevor die Anwendung startet — Windows
    (windows/service.py) und systemd (ExecStartPre=+… update_tausch.py nachholen). None, wenn
    nichts offen war; sonst das Ergebnis von ruecknahme_nachholen. Windows übergibt die eigene
    Wrapper-Fassung (bestätigt einen eingeplanten Neustart)."""
    if wrapper_fassung:
        wrapper_gestartet(daten, wrapper_fassung)
    if not ruecknahme_offen_pfad(daten).exists():
        return None
    return ruecknahme_nachholen(wurzel, daten)


# ── systemd ──────────────────────────────────────────────────────────────────

def _systemctl(*args: str) -> int:
    return subprocess.run(["systemctl", *args], capture_output=True, timeout=120).returncode


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Austausch des EXO SMTP Relay")
    p.add_argument("befehl", choices=("ablauf", "phase", "nachholen"))
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--wurzel", required=True)
    p.add_argument("--daten", default="")
    p.add_argument("--version", default="")
    p.add_argument("--paket", default="")
    p.add_argument("--lauf", default="")
    p.add_argument("--dienst", default="exo-smtp-relay")
    a = p.parse_args(argv)
    wurzel = Path(a.wurzel)
    daten = Path(a.daten) if a.daten else wurzel / "data"
    if a.befehl == "nachholen":
        # systemd ExecStartPre (als root, vor dem Start der Anwendung). Nie den Start blockieren:
        # scheitert es, steht es im Status, und das Relay läuft mit dem, was am Platz ist.
        ergebnis = beim_dienststart(wurzel, daten)
        print({None: "nichts offen", True: "Rücknahme nachgeholt", False: "Rücknahme weiter offen"}[ergebnis])
        return 0
    if a.befehl == "phase":
        try:
            print(phase_ausfuehren(a.name, wurzel, daten, a.version, a.paket, a.lauf))
            return 0
        except Exception as exc:                              # noqa: BLE001
            print(str(exc), file=sys.stderr)
            return 1
    # ablauf — systemd (Windows ruft `ablauf()` direkt aus service.py). Eine offene Rücknahme
    # holt ablauf() vor dem Auftrag nach; ohne Auftrag hier (Aufruf ohne Auftrag, z. B. von Hand).
    if not auftrag_pfad(daten).exists():
        if ruecknahme_offen_pfad(daten).exists():
            _systemctl("stop", a.dienst)
            ruecknahme_nachholen(wurzel, daten)
            _systemctl("start", a.dienst)
        return 0
    ende = ablauf(wurzel, daten,
                  stoppen=lambda: _systemctl("stop", a.dienst),
                  starten=lambda: _systemctl("start", a.dienst),
                  laeuft=lambda: _systemctl("is-active", "--quiet", a.dienst) == 0)
    print(ende)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
