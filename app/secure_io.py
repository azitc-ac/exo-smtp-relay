"""Schreiben von Geheimnissen — EINE Umsetzung für Gateway UND Hub.

DIESE DATEI MUSS IN BEIDEN ANWENDUNGEN INHALTSGLEICH SEIN.
tools/driftcheck.py vergleicht die SHA-256.

WARUM DAS EXISTIERT
-------------------
Das Audit vom 2026-07-26 fand denselben Rechte-Fehler an vier Stellen und
dreierlei Ursachen für dieselbe Wirkung:

  * `data/smime/*/certs/*/key.pem` — S/MIME-PRIVATSCHLÜSSEL — lagen mit 644,
    die Verzeichnisse mit 755.
  * `data/acme/account_key_*.pem` — ACME-Account-Schlüssel — ebenso 644.
  * Der Wiederherstellungspfad in `backup_manager.py` schrieb `auth.pfx`,
    `settings.json` und Privatschlüssel ohne `chmod` zurück — er verschlechterte
    die Rechte also genau dann, wenn ein Betreiber ein Problem behebt.
  * `legal_consent.db` (Zustimmungsbelege) und `mail_audit.db`
    (Mail-Metadaten) — personenbezogene Daten — 644.

Dabei machten `portal_store.py` und `hub_orders.py` es längst richtig (700/600).
Das Muster war vorhanden, es wurde nur nicht angewandt — genau die Art Befund,
die laut CLAUDE.md („Gemeinsame Bausteine") eine Strukturänderung erfordert und
keinen Changelog-Eintrag.

ZWEI FALLEN, DIE HIER EIN FÜR ALLE MAL GELÖST SIND
--------------------------------------------------
1. `rename()` übernimmt die Rechte der QUELLdatei. Wer atomar über eine
   Temp-Datei schreibt und danach das Ziel chmodet, verliert die Rechte beim
   nächsten Speichern. Deshalb: chmod auf der TEMP-Datei, vor dem replace.
2. Ein Verzeichnis mit 755 macht 600-Dateien nur halb sicher: der Name, die
   Existenz und die Größe bleiben lesbar, und ein einziger vergessener chmod
   im Verzeichnis liegt offen. Deshalb wird das Elternverzeichnis mitgehärtet.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

log = logging.getLogger(__name__)

FILE_MODE = 0o600      # nur der Dienstbenutzer
DIR_MODE = 0o700

# Dateinamen-Muster, die unter DATA_DIR grundsätzlich als geheim gelten.
# Wird von harden_tree() für Bestandsdaten genutzt und ist die Grundlage der
# Prüfung in tools/driftcheck.py.
SECRET_GLOBS = (
    "*.pem", "*.key", "*.pfx", "*.p12", "*.jks",
    "settings.json", "settings.bak",
    "*.db",                      # portal.db, mail_audit.db, legal_consent.db
    "orders.json", "customers.json", "hub_settings.json", "licenses.json",
    "account_url_*.txt",
    # certbots ACME-Konto-Schlüssel. Certbot legt ihn selbst mit 400 ab; der
    # Eintrag dient dazu, dass sein Verzeichnis mitgehärtet wird.
    "private_key.json",
)


def ensure_dir(path: Path | str, mode: int = DIR_MODE) -> Path:
    """Verzeichnis anlegen und Rechte durchsetzen (auch wenn es schon existierte)."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    try:
        if stat.S_IMODE(p.stat().st_mode) != mode:
            p.chmod(mode)
    except OSError as exc:                       # z.B. fremder Eigentümer
        log.warning("secure_io: chmod %o auf %s fehlgeschlagen: %s", mode, p, exc)
    return p


def harden_file(path: Path | str, mode: int = FILE_MODE) -> Path:
    """Rechte einer bestehenden Datei durchsetzen — für Dateien, die eine
    fremde Bibliothek selbst anlegt.

    ANLASS (2026-08-25): SQLite legt seine Datenbankdatei selbst an, mit der
    umask des Prozesses — im Container 022, also 644. `harden_tree()` räumt das
    auf, läuft aber nur beim Start. Eine Datenbank, die zur LAUFZEIT entsteht,
    bleibt damit bis zum nächsten Neustart für jeden Systembenutzer lesbar.

    Im Bestand fiel das nicht auf, weil jede vorhandene `.db` längst einen
    Neustart erlebt hat: Ein Blick auf die Rechte im laufenden Betrieb zeigt
    überall 600 und bestätigt scheinbar, dass alles stimmt. Es ist dieselbe
    Klasse wie beim atomaren Schreiben — die Rechte gehören dorthin gesetzt, wo
    die Datei ENTSTEHT, nicht dorthin, wo man später hinsieht.

    Idempotent und leise: Ein fehlgeschlagenes `chmod` (fremder Eigentümer, nur
    lesbar eingehängt) darf den Aufrufer nicht anhalten — es wird protokolliert.
    """
    p = Path(path)
    try:
        if p.exists() and stat.S_IMODE(p.stat().st_mode) != mode:
            p.chmod(mode)
    except OSError as exc:
        log.warning("secure_io: chmod %o auf %s fehlgeschlagen: %s", mode, p, exc)
    return p


def _atomic_write(path: Path, data: bytes, mode: int) -> Path:
    """Atomar schreiben, Rechte auf der TEMP-Datei setzen (siehe Kopfkommentar)."""
    path = Path(path)
    ensure_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.chmod(mode)                              # ZWINGEND vor replace()
    tmp.replace(path)
    return path


def write_secret_bytes(path: Path | str, data: bytes) -> Path:
    """Privatschlüssel, PFX, Datenbanken — 600, Verzeichnis 700, atomar."""
    return _atomic_write(Path(path), data, FILE_MODE)


def write_secret_text(path: Path | str, text: str, encoding: str = "utf-8") -> Path:
    return write_secret_bytes(path, text.encode(encoding))


def write_secret_json(path: Path | str, obj, indent: int = 2) -> Path:
    return write_secret_bytes(
        path, json.dumps(obj, indent=indent, ensure_ascii=False).encode("utf-8"))


def _tightened(current: int) -> int:
    """Nur die Rechte für Gruppe und Andere entfernen, Eigentümer-Bits behalten.

    ZWINGEND so und nicht als absoluter Modus: certbot legt
    `le-config/accounts/.../private_key.json` mit **400** ab. Ein pauschales
    chmod(0o600) würde diese Rechte ERWEITERN. Härten darf nur einschränken —
    sonst verschlechtert die Sicherheitsmaßnahme punktuell die Sicherheit.
    """
    return current & ~0o077


def harden_file(path: Path | str) -> bool:
    """Group/Other-Rechte einer bestehenden Datei entfernen. True, wenn geändert."""
    p = Path(path)
    try:
        if not p.is_file():
            return False
        cur = stat.S_IMODE(p.stat().st_mode)
        new = _tightened(cur)
        if new == cur:
            return False
        p.chmod(new)
        return True
    except OSError as exc:
        log.warning("secure_io: chmod auf %s fehlgeschlagen: %s", p, exc)
        return False


def harden_tree(root: Path | str, globs: tuple[str, ...] = SECRET_GLOBS) -> dict:
    """Bestandsdaten nachträglich härten — beim Start aufzurufen.

    Ohne diesen Schritt bliebe jede bereits ausgelieferte Installation auf den
    alten 644-Rechten stehen: neue Schreibvorgänge wären korrekt, die
    vorhandenen Schlüssel aber weiter lesbar. Idempotent und still, wenn nichts
    zu tun ist.
    """
    root = Path(root)
    if not root.is_dir():
        return {"files": 0, "dirs": 0}
    changed_files = 0
    # Elternverzeichnis JEDER gefundenen Geheimnisdatei, nicht nur der geänderten.
    # Sonst bliebe ein Ordner mit einem bereits korrekten 600er-Schlüssel auf 755
    # stehen — wir würden Änderungen nachziehen statt die Invariante zu erzwingen.
    secret_dirs: set[Path] = set()
    for pattern in globs:
        for f in root.rglob(pattern):
            if not f.is_file():
                continue
            secret_dirs.add(f.parent)
            if harden_file(f):
                changed_files += 1
    touched_dirs = secret_dirs
    # Elternkette bis (exklusive) root einsammeln: eine 600-Datei in einem
    # 755-Verzeichnis ist nur halb geschützt. `root` selbst bleibt absichtlich
    # unangetastet — es ist der Einhängepunkt des Bind-Mounts, und seine Rechte
    # gehören dem Betreiber, nicht uns.
    to_harden: set[Path] = set()
    for d in touched_dirs:
        cur = d
        while cur != root and root in cur.parents:
            to_harden.add(cur)
            cur = cur.parent
    changed_dirs = 0
    for d in sorted(to_harden):
        try:
            cur = stat.S_IMODE(d.stat().st_mode)
            new = _tightened(cur)
            if new != cur:
                d.chmod(new)
                changed_dirs += 1
        except OSError as exc:
            log.warning("secure_io: chmod auf %s fehlgeschlagen: %s", d, exc)
    if changed_files or changed_dirs:
        log.warning("secure_io: %d Datei(en) und %d Verzeichnis(se) unter %s auf "
                    "600/700 korrigiert (waren zu offen)",
                    changed_files, changed_dirs, root)
    return {"files": changed_files, "dirs": changed_dirs}


def audit_tree(root: Path | str, globs: tuple[str, ...] = SECRET_GLOBS) -> list[tuple[str, str]]:
    """Nur melden, nicht ändern — für Diagnoseseiten und Tests.
    Liefert [(pfad, "644"), …] für alles, was zu offen ist."""
    root = Path(root)
    out: list[tuple[str, str]] = []
    if not root.is_dir():
        return out
    for pattern in globs:
        for f in root.rglob(pattern):
            try:
                m = stat.S_IMODE(f.stat().st_mode)
            except OSError:
                continue
            if m & 0o077:                        # irgendein Recht für group/other
                out.append((str(f), oct(m)[-3:]))
    return sorted(set(out))


def safe_join(base: Path | str, relative: str) -> Path | None:
    """Zielpfad innerhalb von `base` auflösen — oder None, wenn er ausbricht.

    Für das Auspacken von Archiven (Backup-Wiederherstellung, Zip-Slip). Beide
    Anwendungen prüften bisher mit
        str(target).startswith(str(base.resolve()))
    Das ist ein PRÄFIXVERGLEICH AUF ZEICHENKETTEN und lässt Geschwisterpfade
    durch, die zufällig denselben Anfang haben:
        (/app/data / "../data-evil/x").resolve() == /app/data-evil/x
        startswith("/app/data") -> True, obwohl ausserhalb.
    In der jetzigen Verzeichnisaufteilung war das nicht ausnutzbar (unter
    /app/ beginnt kein sicherheitsrelevanter Pfad mit "data"), aber es ist die
    falsche Prüfung — und sie stand wortgleich in beiden Anwendungen.
    `is_relative_to()` vergleicht Pfadbestandteile statt Zeichen.
    """
    base_r = Path(base).resolve()
    target = (base_r / relative).resolve()
    return target if target.is_relative_to(base_r) else None
