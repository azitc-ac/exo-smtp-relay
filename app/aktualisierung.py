"""Aktualisierung aus fertigen Releases — Prüfen, Herunterladen, Auftrag.

Anders als im Gateway (git pull + Neubau über den update-watcher) kommen hier
FERTIGE, signierte Release-Pakete von GitHub. Der Webprozess

  1. fragt die GitHub-Releases-API nach der jüngsten Fassung des gewählten
     Kanals (`pruefen`, von Hand oder im Zeitplaner),
  2. lädt beim Einspielen Paket, SHA256SUMS und SHA256SUMS.sig nach
     `data/update/`, prüft Signatur und Prüfsumme (`update_paket`) und schreibt
     erst DANN den Auftrag (`einspielen`),
  3. tauscht SELBST NICHTS aus. Das macht `update_tausch.py` mit den nötigen
     Rechten (Windows: Dienst-Wrapper; Linux: eigene systemd-Unit) und prüft
     dabei ein zweites Mal.

KANÄLE (Gateway: release/nightly/main — nicht übertragbar, weil es hier nur
Releases gibt): `stable` nimmt nur Releases ohne Suffix, `beta` zusätzlich
Vorabfassungen mit `-beta[.N]` — jeweils die höchste Versionsnummer.

Docker tauscht sich nicht selbst aus; dort zeigt die Seite den Befehl.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import config
import update_paket
import tls_vertrauen
import update_tausch

log = logging.getLogger(__name__)

REPO = "azitc-ac/exo-smtp-relay"
API_RELEASES = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
IMAGE = f"ghcr.io/{REPO}"
KANAELE = ("stable", "beta")
INTERVALLE_H = (0, 6, 24, 168)          # 0 = keine planmässige Prüfung
MAX_DOWNLOAD = 100 * 1024 * 1024
BETRIEBSARTEN = ("docker", "windows", "systemd", "manuell")
SYSTEMD_TAUSCHER = "exo-smtp-relay-update.path"


def _daten() -> Path:
    return Path(config.DATA_DIR)


def _bereich() -> Path:
    return update_tausch.bereich(_daten())


def _stand_pfad() -> Path:
    return _bereich() / "stand.json"


# ── HTTP (in Tests ersetzt) ──────────────────────────────────────────────────

def _laden(url: str, accept: str = "application/vnd.github+json",
           max_bytes: int = 2 * 1024 * 1024) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"EXO-SMTP-Relay/{config.VERSION}",
                                               "Accept": accept})
    # Nur https-Adressen aus der GitHub-API bzw. API_RELEASES.
    # Kontext mit Systemspeicher UND certifi — frisches Windows kennt die
    # GitHub-Wurzel sonst evtl. noch nicht (tls_vertrauen).
    ctx = tls_vertrauen.client_kontext()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:     # nosec B310
        daten = r.read(max_bytes + 1)
    if len(daten) > max_bytes:
        raise ValueError(f"Antwort grösser als {max_bytes // (1024 * 1024)} MB")
    return daten


# ── Betriebsart ──────────────────────────────────────────────────────────────

def betriebsart() -> str:
    """docker | windows | systemd | manuell.

    Ausdrücklich gesetzt von Dockerfile, windows/service.py und der systemd-
    Unit (RELAY_BETRIEBSART). Rückfall für ältere Installationen: /.dockerenv
    bzw. die INVOCATION_ID, die systemd jedem Dienst mitgibt."""
    art = (os.environ.get("RELAY_BETRIEBSART") or "").strip().lower()
    if art in BETRIEBSARTEN:
        return art
    if Path("/.dockerenv").exists():
        return "docker"
    if sys.platform != "win32" and os.environ.get("INVOCATION_ID"):
        return "systemd"
    return "manuell"


def tauscher_bereit(art: str | None = None) -> tuple[bool, str]:
    """Kann der Tausch auf DIESER Maschine laufen? (bereit, Hinweis)"""
    art = art or betriebsart()
    if art == "docker":
        return False, "Im Container tauscht sich das Relay nicht selbst aus."
    if art == "manuell":
        return False, ("Das Relay läuft weder als Windows-Dienst noch unter systemd — "
                       "das Paket bitte von Hand einspielen.")
    if art == "windows":
        w = update_tausch.json_lesen(_bereich() / "waechter.json")
        if int(w.get("protokoll") or 0) >= update_tausch.PROTOKOLL:
            return True, ""
        return False, ("Der Dienst-Wrapper ist älter als der Updater. Einmal "
                       "`windows\\install.ps1` aus dem neuen Paket ausführen — danach "
                       "geht es über diesen Knopf.")
    try:
        import subprocess
        r = subprocess.run(["systemctl", "is-enabled", SYSTEMD_TAUSCHER],
                           capture_output=True, text=True, timeout=10)
        if r.stdout.strip() == "enabled":
            return True, ""
    except Exception:                                         # noqa: BLE001
        pass
    return False, (f"{SYSTEMD_TAUSCHER} ist nicht eingerichtet — siehe Kopf von "
                   "linux/exo-smtp-relay-update.service.")


def paket_art(art: str | None = None) -> str:
    return "windows" if (art or betriebsart()) == "windows" else "linux"


# ── Prüfen ───────────────────────────────────────────────────────────────────

def _jetzt() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def neuestes_release(releases: list, kanal: str) -> dict | None:
    """Höchste Version des Kanals. Entwürfe, fremde Tags und Releases, deren
    Kanal nicht zum Tag passt, zählen nicht."""
    beste, beste_k = None, None
    for rel in releases or []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        tag = str(rel.get("tag_name") or "")
        if not tag.startswith("v"):
            continue
        version = tag[1:]
        k, rk = update_paket.version_schluessel(version), update_paket.kanal_von(version)
        if k is None or (kanal == "stable" and (rk != "stable" or rel.get("prerelease"))):
            continue
        if beste_k is None or k > beste_k:
            beste, beste_k = rel, k
    return beste


def pruefen(kanal: str | None = None) -> dict:
    """Jüngste Fassung des Kanals erfragen und in data/update/stand.json merken."""
    import settings_store
    kanal = kanal or settings_store.get("UPDATE_KANAL") or "stable"
    if kanal not in KANAELE:
        return {"ok": False, "error": f"Unbekannter Kanal {kanal!r}"}
    aktuell = config.VERSION
    stand: dict = {"ok": True, "kanal": kanal, "aktuell": aktuell, "geprueft": _jetzt()}
    try:
        releases = json.loads(_laden(API_RELEASES))
        rel = neuestes_release(releases, kanal)
        if rel is None:
            stand.update(neueste=None, verfuegbar=False, hinweis="Noch kein Release in diesem Kanal.")
        else:
            neueste = rel["tag_name"][1:]
            k_neu, k_akt = (update_paket.version_schluessel(neueste),
                            update_paket.version_schluessel(aktuell))
            stand.update(
                neueste=neueste, url=rel.get("html_url", ""),
                veroeffentlicht=rel.get("published_at", ""),
                verfuegbar=bool(k_akt is None or k_neu > k_akt),
                assets={a["name"]: a["browser_download_url"] for a in rel.get("assets") or []
                        if a.get("name") and a.get("browser_download_url")})
    except urllib.error.HTTPError as exc:
        stand = {"ok": False, "kanal": kanal, "aktuell": aktuell, "geprueft": _jetzt(),
                 "error": f"GitHub: HTTP {exc.code} {exc.reason}"}
    except Exception as exc:                                  # noqa: BLE001
        stand = {"ok": False, "kanal": kanal, "aktuell": aktuell, "geprueft": _jetzt(),
                 "error": f"GitHub nicht erreichbar: {exc}"}
    try:
        update_tausch.json_schreiben(_stand_pfad(), stand)
    except OSError as exc:
        log.warning("Aktualisierung: Stand nicht gespeichert: %s", exc)
    if stand.get("verfuegbar"):
        log.info("Aktualisierung: %s verfügbar (Kanal %s, laufend %s)",
                 stand["neueste"], kanal, aktuell)
    return stand


def stand() -> dict:
    """Letztes Prüfergebnis — ohne Netz. Gilt nur, solange es zur laufenden
    Fassung und zum eingestellten Kanal gehört."""
    import settings_store
    s = update_tausch.json_lesen(_stand_pfad())
    if s.get("aktuell") != config.VERSION or s.get("kanal") != (settings_store.get("UPDATE_KANAL") or "stable"):
        return {}
    return s


def verfuegbare_version() -> str | None:
    """Für das Band: die neuere Version, sonst None."""
    s = stand()
    return s.get("neueste") if s.get("ok") and s.get("verfuegbar") else None


def planmaessig_pruefen() -> bool:
    """Aus dem stündlichen Zeitplaner. True, wenn geprüft wurde."""
    import settings_store
    stunden = int(settings_store.get("UPDATE_PRUEF_STUNDEN") or 0)
    if stunden <= 0:
        return False
    zuletzt = update_tausch.json_lesen(_stand_pfad())
    try:
        alt = datetime.strptime(zuletzt.get("geprueft", ""), "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - alt).total_seconds() < stunden * 3600 - 300 \
                and zuletzt.get("aktuell") == config.VERSION \
                and zuletzt.get("kanal") == settings_store.get("UPDATE_KANAL"):
            return False
    except ValueError:
        pass
    pruefen()
    return True


# ── Einspielen ───────────────────────────────────────────────────────────────

def status() -> dict:
    return update_tausch.json_lesen(update_tausch.status_pfad(_daten())) or {"zustand": "ruhe"}


DOCKER_BEFEHL = "docker compose pull && docker compose up -d"


def einspielen(angefordert_von: str) -> dict:
    """Paket laden, prüfen, Auftrag schreiben. Tauscht selbst nichts aus."""
    art = betriebsart()
    bereit, hinweis = tauscher_bereit(art)
    if not bereit:
        return {"ok": False, "error": hinweis}
    if not update_paket.RELEASE_SCHLUESSEL:
        return {"ok": False, "error": "Kein Prüfschlüssel eingebaut — Einspielen gesperrt."}
    if status().get("zustand") in ("wartet", "laeuft") and update_tausch.auftrag_pfad(_daten()).exists():
        return {"ok": False, "error": "Eine Aktualisierung läuft bereits."}
    s = stand()
    if not (s.get("ok") and s.get("verfuegbar") and s.get("neueste")):
        return {"ok": False, "error": "Keine neuere Fassung bekannt — bitte zuerst prüfen."}
    version = s["neueste"]
    paket = update_paket.paket_name(version, paket_art(art))
    assets = s.get("assets") or {}
    fehlend = [n for n in (paket, update_paket.SUMMEN, update_paket.SIGNATUR) if n not in assets]
    if fehlend:
        return {"ok": False, "error": "Im Release fehlt: " + ", ".join(fehlend)
                + (" — ohne Signatur wird nichts eingespielt." if update_paket.SIGNATUR in fehlend else "")}

    b = _bereich()
    b.mkdir(parents=True, exist_ok=True)
    geladen = []
    try:
        for name, grenze in ((update_paket.SUMMEN, 64 * 1024), (update_paket.SIGNATUR, 4096),
                             (paket, MAX_DOWNLOAD)):
            inhalt = _laden(assets[name], accept="application/octet-stream", max_bytes=grenze)
            (b / name).write_bytes(inhalt)
            geladen.append(b / name)
        update_paket.paket_pruefen(b / paket, (b / update_paket.SUMMEN).read_bytes(),
                                   (b / update_paket.SIGNATUR).read_bytes())
    except update_paket.PaketFehler as exc:
        for p in geladen:
            p.unlink(missing_ok=True)
        log.error("Aktualisierung auf %s abgelehnt: %s", version, exc)
        update_tausch.status_setzen(_daten(), "fehler", f"Abgelehnt: {exc}",
                                    von=config.VERSION, nach=version)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:                                  # noqa: BLE001
        for p in geladen:
            p.unlink(missing_ok=True)
        return {"ok": False, "error": f"Herunterladen fehlgeschlagen: {exc}"}

    update_tausch.status_setzen(_daten(), "wartet", "Auftrag an den Tauscher übergeben",
                                von=config.VERSION, nach=version,
                                angefordert_von=angefordert_von)
    update_tausch.json_schreiben(update_tausch.auftrag_pfad(_daten()), {
        "version": version, "paket": paket, "angefordert_von": angefordert_von,
        "zeit": _jetzt(), "protokoll": update_tausch.PROTOKOLL})
    log.warning("Aktualisierung auf %s durch %s angefordert (%s)", version, angefordert_von, art)
    return {"ok": True, "version": version}


def zustand() -> dict:
    """Alles für die Seite /aktualisierung."""
    import settings_store
    art = betriebsart()
    bereit, hinweis = tauscher_bereit(art)
    kanal = settings_store.get("UPDATE_KANAL") or "stable"
    return {"ok": True, "aktuell": config.VERSION, "betriebsart": art,
            "bereit": bereit, "hinweis": hinweis,
            "schluessel": bool(update_paket.RELEASE_SCHLUESSEL),
            "kanal": kanal, "intervall": int(settings_store.get("UPDATE_PRUEF_STUNDEN") or 0),
            "stand": stand(), "status": status(),
            "docker": {"image": f"{IMAGE}:{kanal}", "befehl": DOCKER_BEFEHL},
            "zeit": time.time()}
