"""
Backup und Wiederherstellung aller persistenten Relay-Daten (v0.2.35).

Übernommen aus dem Gateway (backup_manager.py, Stand f0c95af) — gleiche
Funktionsnamen, gleiche Verschlüsselung, ohne Signaturvorlagen, S/MIME, ACME
und Bookings-Abgleich, die es im Relay nicht gibt.

Backup-Inhalt (ZIP), alles unter `data/`:
  settings.json     — Einstellungen samt Geheimnissen (Anmeldekennwort als
                      Hash, Sitzungsschlüssel, Zugangsdaten des Rückwegs) und
                      dem Rechte-Stand der App (RECHTE_*)
  relay_hosts.db    — Geräte-/Host-Freigaben, Sperren, Zähler, Abweisungen
  certs/cert.pem    — TLS-Zertifikat des Listeners
  certs/key.pem     — sein privater Schlüssel
  auth.pfx          — Anmeldezertifikat der App bei Exchange Online
  mail_audit.db     — Mail-Protokoll
  mailboxes.json    — letzter Stand der Postfachliste

  tls_dns01_pending.json — offene DNS-01-Bestellung bei Let's Encrypt samt
                      Konto- und Domain-Schlüssel (nur während einer Bestellung)

Nicht enthalten: logs/ (Laufzeit-Logs), settings.bak und Zwischendateien
(*.tmp, SQLite-Journale) — und certbots Verzeichnisse le-config/, le-work/,
le-logs/ sowie acme-webroot/ (wie im Gateway). le-config/ besteht aus
Symlinks (live/ → archive/); als Datei ins ZIP gelesen kämen sie als Kopien
zurück, und `certbot renew` bricht an „expected … to be a symlink“ ab. Das
Zertifikat selbst ist über certs/ enthalten. Nach einer Wiederherstellung
stellt der Zeitplaner bei fälligem Ablauf mit den gespeicherten Angaben
(LE_DOMAIN, LE_EMAIL, LE_LAUF.weg = http01) neu aus (scheduler._tls_pruefen).

TLS-ZERTIFIKAT: Es liegt dort, wo `config.SMTP_TLS_CERT` / `SMTP_TLS_KEY`
hinzeigen — ohne Angabe unter DATA_DIR/certs/, per Umgebung auch woanders. Im
Archiv steht es IMMER als data/certs/cert.pem bzw. key.pem und wird an den
Pfad zurückgeschrieben, den das Zielsystem konfiguriert hat. So überlebt ein
Umzug zwischen Docker (/app/data), Linux (/opt/exo-smtp-relay/data) und
Windows (C:\\ProgramData\\exo-smtp-relay\\data) auch abweichende Zertifikatspfade.

ZIP-NAMEN immer mit „/“ (`_zip_name`): `str(Path.relative_to())` ergäbe unter
Windows „data/certs\\cert.pem“ — ein solches Archiv ließe sich auf Linux nicht
zuordnen, und die Auswahl beim Wiederherstellen verfehlte die Datei.

SQLITE: Datenbanken werden über die Backup-Schnittstelle von SQLite gelesen,
nicht als Datei kopiert. Eine Kopie während eines Schreibvorgangs kann einen
halben Stand enthalten.

PASSWORT (optional, wie im Gateway seit v1.11.2)
------------------------------------------------
Das ZIP enthält alle Geheimnisse des Relays im Klartext. Mit Passwort wird es
als Ganzes verschlüsselt (`verschluesseln`): AES-256-GCM, Schlüssel per scrypt
aus dem Passwort, Datei `*.relaybak`. Ohne Passwort bleibt es ein ZIP — ein
Backup mit vergessenem Passwort ist wertlos, das Passwort ist deshalb ein
Angebot, keine Pflicht; die Oberfläche warnt deutlich.
Wiederherstellen erkennt die Art am Dateianfang (`MAGIE`), nicht am Namen.
Die Magie unterscheidet sich vom Gateway (`EXOGW-BACKUP-1`): Ein Relay-Backup
ist kein Gateway-Backup und umgekehrt.

RECHTE: Jede wiederhergestellte Datei geht über `secure_io.write_secret_bytes`
(Datenbanken über `_db_schreiben` + `harden_file`)
(600, Verzeichnis 700; unter Windows erbt sie die ACL von data\\, die
install.ps1 auf SYSTEM und Administratoren beschränkt). Danach härtet
`secure_io.harden_tree` den Bestand nach.
"""

import io
import json
import logging
import re
import secrets
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePath

import config

import secure_io

log = logging.getLogger(__name__)

DATA_DIR = Path(config.DATA_DIR)

# Kennzeichen eines Relay-Backups. Das Gateway prüft nur auf
# data/settings.json — die hat auch das Relay. Erst diese Datei trennt beide.
MANIFEST = "relay-backup.json"
ART = "exo-smtp-relay"

# update/: heruntergeladene Release-Pakete und Zustand des Tauschers
# (aktualisierung.py) — gehören zur Maschine, nicht zu den Daten des Relays.
_EXCLUDE_DATA_SUBDIRS = {"logs", "le-config", "le-work", "le-logs", "acme-webroot", "update"}
_EXCLUDE_DATA_FILES = {"settings.bak"}
_EXCLUDE_ENDUNGEN = (".tmp", "-journal", "-wal", "-shm")

# Feste Archivnamen des TLS-Paares → Pfad auf DIESEM System (zur Laufzeit
# gelesen, damit eine Umgebungsangabe gilt).
TLS_CERT_NAME = "data/certs/cert.pem"
TLS_KEY_NAME = "data/certs/key.pem"


def _tls_ziele() -> dict[str, Path]:
    return {TLS_CERT_NAME: Path(config.SMTP_TLS_CERT), TLS_KEY_NAME: Path(config.SMTP_TLS_KEY)}


def _zip_name(rel: PurePath) -> str:
    """Relativer Pfad → Archivname, immer mit „/“ (siehe Modulkopf)."""
    return "data/" + rel.as_posix()


# ── Verschlüsselung (wie Gateway) ────────────────────────────────────────────
MAGIE = b"EXORELAY-BACKUP-1\n"
_SALZ, _NONCE = 16, 12
# scrypt: 2^15 × 8 × 128 B = 32 MB Speicher je Ableitung — tragbar auch auf
# kleinen Maschinen, teuer genug gegen Raten.
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
MIN_PASSWORT = 8


class PasswortFehler(ValueError):
    """Passwort fehlt, ist falsch, oder die Datei ist beschädigt."""


def _schluessel(passwort: str, salz: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salz, length=32, **_SCRYPT).derive(passwort.encode("utf-8"))


def ist_verschluesselt(daten: bytes) -> bool:
    return daten[:len(MAGIE)] == MAGIE


def verschluesseln(zip_bytes: bytes, passwort: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(passwort or "") < MIN_PASSWORT:
        raise PasswortFehler(f"Passwort zu kurz (mindestens {MIN_PASSWORT} Zeichen).")
    salz, nonce = secrets.token_bytes(_SALZ), secrets.token_bytes(_NONCE)
    # Kopf (Magie+Salz+Nonce) als zugehörige Daten: verändert ihn jemand,
    # scheitert die Entschlüsselung, statt still einen anderen Schlüssel zu nehmen.
    kopf = MAGIE + salz + nonce
    return kopf + AESGCM(_schluessel(passwort, salz)).encrypt(nonce, zip_bytes, kopf)


def entschluesseln(daten: bytes, passwort: str) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not passwort:
        raise PasswortFehler("Dieses Backup ist verschlüsselt — Passwort angeben.")
    a = len(MAGIE)
    salz, nonce = daten[a:a + _SALZ], daten[a + _SALZ:a + _SALZ + _NONCE]
    kopf = daten[:a + _SALZ + _NONCE]
    try:
        return AESGCM(_schluessel(passwort, salz)).decrypt(nonce, daten[len(kopf):], kopf)
    except (InvalidTag, ValueError):
        raise PasswortFehler("Passwort falsch oder Datei beschädigt.") from None


def oeffnen(daten: bytes, passwort: str = "") -> bytes:
    """Hochgeladene Datei → ZIP-Bytes. Unverschlüsselt: unverändert."""
    return entschluesseln(daten, passwort) if ist_verschluesselt(daten) else daten


# ── Erstellen ────────────────────────────────────────────────────────────────

def _db_lesen(pfad: Path) -> bytes:
    """Konsistenter Stand einer SQLite-Datenbank (Backup-Schnittstelle).
    Keine gültige Datenbank → die Datei, wie sie ist."""
    with tempfile.TemporaryDirectory() as tmp:
        ziel = Path(tmp) / "kopie.db"
        try:
            # as_uri(): unter Windows file:///C:/..., Sonderzeichen maskiert.
            quelle = sqlite3.connect(pfad.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                kopie = sqlite3.connect(str(ziel))
                try:
                    quelle.backup(kopie)
                finally:
                    kopie.close()
            finally:
                quelle.close()
            return ziel.read_bytes()
        except sqlite3.Error as exc:
            log.warning("Backup: %s nicht über SQLite lesbar (%s) — Datei wird kopiert", pfad, exc)
            return pfad.read_bytes()


def _db_schreiben(target: Path, inhalt: bytes) -> None:
    """Datenbank zurückspielen — über SQLite IN die bestehende Datei, nicht per
    Dateitausch.

    ANLASS (CI unter Windows, 24.11.2026): `os.replace()` auf relay_hosts.db
    scheiterte mit WinError 5/32, solange der Dienst eine Verbindung offen hat.
    Unter Windows lässt sich eine geöffnete Datei nicht ersetzen; unter Linux
    gelänge der Tausch zwar, offene Verbindungen schrieben aber in die alte,
    gelöschte Datei weiter. Die Backup-Schnittstelle von SQLite nimmt die
    Sperren der Datenbank und ersetzt den Inhalt — auf beiden Systemen, und
    die Datei behält ihre Rechte bzw. ACL."""
    secure_io.ensure_dir(target.parent)
    with tempfile.TemporaryDirectory() as tmp:
        quelle_pfad = Path(tmp) / "quelle.db"
        quelle_pfad.write_bytes(inhalt)
        quelle = sqlite3.connect(str(quelle_pfad))
        try:
            ziel = sqlite3.connect(str(target))
            try:
                quelle.backup(ziel)
            finally:
                ziel.close()
        finally:
            quelle.close()
    secure_io.harden_file(target)       # neu angelegt: umask-Rechte → 600


def _ausgeschlossen(rel: PurePath) -> bool:
    teile = rel.parts
    if teile and teile[0] in _EXCLUDE_DATA_SUBDIRS:
        return True
    return rel.as_posix() in _EXCLUDE_DATA_FILES or rel.name.endswith(_EXCLUDE_ENDUNGEN)


def create_backup(passwort: str = "") -> tuple[bytes, str]:
    """Erstellt vollständiges ZIP-Backup. Returns (bytes, filename) — mit
    *passwort* verschlüsselt (`*.relaybak`), sonst ein ZIP."""
    import socket
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rand = secrets.token_hex(2)
    safe_host = re.sub(r"[^a-z0-9]+", "-", socket.gethostname().lower())[:20].strip("-") or "relay"
    filename = f"relay-backup-{safe_host}-{ts}-{rand}.zip"
    erstellt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    tls = _tls_ziele()
    tls_pfade = {p.resolve() for p in tls.values()}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST, json.dumps(
            {"art": ART, "version": config.VERSION, "erstellt": erstellt,
             "verschluesselt": bool(passwort)}, indent=2))
        zf.writestr("README.txt", (
            f"EXO SMTP Relay — Backup\n"
            f"Erstellt: {erstellt}\n"
            f"Host:     {socket.gethostname()}\n"
            f"Version:  {config.VERSION}\n"
            f"\n"
            f"Inhalt (data/):\n"
            f"  settings.json   — Einstellungen samt Geheimnissen\n"
            f"  relay_hosts.db  — Geräte-/Host-Freigaben\n"
            f"  certs/          — TLS-Zertifikat und privater Schlüssel\n"
            f"  auth.pfx        — Anmeldezertifikat bei Exchange Online\n"
            f"  mail_audit.db   — Mail-Protokoll\n"
            f"\n"
            + ("" if passwort else
               "ACHTUNG: Dieses Archiv ist NICHT verschlüsselt. Es enthält private\n"
               "Schlüssel und Zugangsdaten im Klartext. Aufbewahren wie die Schlüssel selbst.\n\n")
            + "Wiederherstellung:\n"
              "  Weboberfläche → Backup → Datei hochladen → Dienst neu starten\n"
              "  (Windows: Dienst ExoSmtpRelay, Docker: docker compose restart).\n"
        ))

        if DATA_DIR.exists():
            for f in sorted(DATA_DIR.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(DATA_DIR)
                if _ausgeschlossen(rel) or f.resolve() in tls_pfade:
                    continue
                inhalt = _db_lesen(f) if f.suffix == ".db" else f.read_bytes()
                zf.writestr(_zip_name(rel), inhalt)

        for name, pfad in tls.items():
            if pfad.is_file():
                zf.writestr(name, pfad.read_bytes())

    daten = buf.getvalue()
    if passwort:
        daten = verschluesseln(daten, passwort)
        filename = filename[:-len(".zip")] + ".relaybak"
    log.info("Backup erstellt: %s (%d KB, %s)", filename, len(daten) // 1024,
             "verschlüsselt" if passwort else "unverschlüsselt")
    return daten, filename


def validate_backup(zip_bytes: bytes) -> list[str]:
    """Prüft Grundstruktur. Returns Fehlerliste (leer = OK)."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            if MANIFEST not in names:
                return ["Kein Relay-Backup (ein Backup des Signature Gateway lässt sich "
                        "hier nicht einspielen)."]
            try:
                art = json.loads(zf.read(MANIFEST)).get("art")
            except Exception:                                 # noqa: BLE001
                art = None
            if art != ART:
                return ["Kein Relay-Backup: Kennzeichen fehlt oder ist fremd."]
            if "data/settings.json" not in names:
                return ["Kein gültiges Relay-Backup: data/settings.json fehlt."]
            return []
    except zipfile.BadZipFile:
        return ["Ungültige ZIP-Datei."]
    except Exception as exc:                                  # noqa: BLE001
        return [f"Lesefehler: {exc}"]


# Anzeigenamen der data/-Bereiche. Wer wiederherstellt, denkt in Dingen
# („die Geräteliste", „die Einstellungen") und nicht in Dateinamen.
_BEREICHE: list[tuple[str, str, str]] = [
    # (Schlüssel, Titel, Erläuterung)
    ("einstellungen", "Einstellungen",
     "settings.json — Rückweg, Tenant, Adressquelle, Anmeldung, Rechte der App"),
    ("geraete", "Geräteliste",
     "relay_hosts.db — freigegebene Geräte und Hosts, Sperren, Abweisungen"),
    ("tls", "TLS-Zertifikat des Listeners",
     "Zertifikat und privater Schlüssel, ggf. offene DNS-01-Bestellung bei Let's Encrypt"),
    ("auth", "Exchange-Anmeldezertifikat",
     "auth.pfx — Anmeldung der App bei Exchange Online"),
    ("datenbanken", "Mail-Protokoll",
     "mail_audit.db — Einlieferungen, Abweisungen, Zustellfehler"),
    ("sonstiges", "Weitere Betriebsdaten",
     "Postfachliste (letzter Stand) und Ähnliches"),
]


def _bereich_von(rel: str) -> str:
    """data/-Pfad → Bereichsschlüssel."""
    if rel == "settings.json":
        return "einstellungen"
    if rel == "relay_hosts.db":
        return "geraete"
    if "data/" + rel in (TLS_CERT_NAME, TLS_KEY_NAME) or rel == "tls_dns01_pending.json":
        return "tls"
    if rel == "auth.pfx":
        return "auth"
    if rel.endswith(".db"):
        return "datenbanken"
    return "sonstiges"


def inspect_backup(zip_bytes: bytes) -> dict:
    """Inhalt eines Backups als Baum — OHNE etwas zu schreiben.

    Grundlage für die Auswahl beim Wiederherstellen. Zertifikat und Schlüssel
    sind EIN Eintrag: einzeln zurückgeholt passten sie nicht zusammen.

    Returns {"ok": bool, "error": str, "gruppen": [...]}, wobei jede Gruppe
    `eintraege` mit `dateien` (den echten ZIP-Namen) führt.
    """
    errors = validate_backup(zip_bytes)
    if errors:
        return {"ok": False, "error": errors[0], "gruppen": []}

    bereiche: dict[str, list[dict]] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for entry in zf.infolist():
            name = entry.filename
            if entry.is_dir() or not name.startswith("data/"):
                continue
            rel = name[len("data/"):]
            if _ausgeschlossen(PurePath(rel)):
                continue
            bereiche.setdefault(_bereich_von(rel), []).append(
                {"name": name, "bytes": entry.file_size})

    eintraege = []
    for schluessel, titel, erlaeuterung in _BEREICHE:
        dateien = bereiche.get(schluessel) or []
        if not dateien:
            continue
        eintraege.append({
            "schluessel": f"daten:{schluessel}",
            "titel": titel,
            "hinweis": erlaeuterung,
            "dateien": [d["name"] for d in dateien],
            "bytes": sum(d["bytes"] for d in dateien),
        })
    gruppen = [{"schluessel": "daten", "titel": "Konfiguration und Daten",
                "eintraege": eintraege}] if eintraege else []
    return {"ok": True, "error": "", "gruppen": gruppen}


# ── Wiederherstellen ─────────────────────────────────────────────────────────

def _ziel(name: str) -> Path | None:
    """ZIP-Name → Zielpfad auf diesem System, oder None (ungültig/ausgeschlossen)."""
    tls = _tls_ziele()
    if name in tls:
        return tls[name]
    rel = name[len("data/"):]
    if _ausgeschlossen(PurePath(rel)):
        return None
    # Zip-Slip-Schutz über secure_io.safe_join (Bestandteile, nicht Präfix).
    return secure_io.safe_join(DATA_DIR, rel)


def restore_backup(zip_bytes: bytes, auswahl: list[str] | None = None) -> dict:
    """
    Stellt Backup wieder her.

    `auswahl` = Liste von ZIP-Namen. `None` bedeutet ALLES. Eine LEERE Liste
    ist dagegen eine Aussage („nichts ausgewählt") und wird abgelehnt, statt
    kommentarlos ein Nichts-Ergebnis zu melden.

    Returns {"ok": bool, "restored_files": int, "warnings": list[str], "error": str}
    """
    errors = validate_backup(zip_bytes)
    if errors:
        return {"ok": False, "error": errors[0], "restored_files": 0, "warnings": []}

    gewaehlt: set[str] | None = None
    if auswahl is not None:
        gewaehlt = {str(n) for n in auswahl}
        if not gewaehlt:
            return {"ok": False, "error": "Nichts ausgewählt — nichts wiederhergestellt.",
                    "restored_files": 0, "warnings": []}

    warnings: list[str] = []
    restored = 0

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            namen = [e.filename for e in zf.infolist()
                     if not e.is_dir() and e.filename.startswith("data/")]
            # settings.json ZULETZT schreiben: Sie ist der Konsistenz-Anker.
            # Scheitert vorher ein anderer Schreibvorgang, bleibt die alte
            # settings.json unberührt — kein Halbzustand.
            namen.sort(key=lambda n: n == "data/settings.json")
            for name in namen:
                # Die Auswahl wirkt NUR einschränkend; die Schranken in _ziel
                # (Zip-Slip, Ausschlüsse) gelten unabhängig davon.
                if gewaehlt is not None and name not in gewaehlt:
                    continue
                target = _ziel(name)
                if target is None:
                    warnings.append(f"Übersprungen (ungültiger Pfad): {name}")
                    continue
                # ZWINGEND über secure_io: 600/700 bzw. die ACL von data\ —
                # eine Wiederherstellung darf die Rechte nicht verschlechtern.
                inhalt = zf.read(name)
                if target.suffix == ".db":
                    try:
                        _db_schreiben(target, inhalt)
                    except sqlite3.DatabaseError as exc:
                        warnings.append(f"Übersprungen (keine gültige Datenbank): {name} — {exc}")
                        continue
                else:
                    secure_io.write_secret_bytes(target, inhalt)
                restored += 1
                if name == "data/auth.pfx":
                    warnings.append(
                        "AUTH_PFX_RESTORED: auth.pfx wurde aus dem Backup wiederhergestellt — "
                        "bitte in der Entra-App-Registrierung prüfen, ob dieses Zertifikat noch "
                        "hinterlegt ist (Einstellungen → Exchange Online).")
                if name == "data/settings.json":
                    warnings.append(
                        "Die Einstellungen des gesicherten Relays gelten jetzt — auch sein "
                        "Anmeldekennwort. Die laufende Sitzung endet.")

        try:
            secure_io.harden_tree(DATA_DIR)
        except Exception as exc:                              # noqa: BLE001
            warnings.append(f"Rechte nicht nachgezogen: {exc}")

        # Einstellungen live neu laden
        try:
            import settings_store
            settings_store.init(getattr(config, "_ENV_SEEDS", None))
        except Exception as exc:                              # noqa: BLE001
            warnings.append(f"Einstellungen neu laden: {exc} — Neustart empfohlen.")

        log.info("Backup wiederhergestellt: %d Dateien, %d Hinweise", restored, len(warnings))
        return {"ok": True, "restored_files": restored, "warnings": warnings, "error": ""}

    except Exception as exc:                                  # noqa: BLE001
        log.error("Backup-Wiederherstellung fehlgeschlagen: %s", exc)
        return {"ok": False, "error": str(exc), "restored_files": restored, "warnings": warnings}
