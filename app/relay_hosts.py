"""Welches Gerät darf einliefern — und was hat es zuletzt getan?

ANLASS (2026-08-25)
-------------------
Das Relay aus v1.8.3 kannte nur Quellnetze. Für den Betrieb ist das zu grob:
Wer ein `/24` freigibt, weiss nicht, welche Geräte darin tatsächlich senden,
kann keinem einzelnen etwas erlauben oder verbieten, und merkt nicht, wenn ein
Kopierer seit drei Monaten schweigt, weil ihn jemand ausgetauscht hat.

Diese Tabelle IST deshalb die Freigabe. Ein Netz sagt nur noch, woraus der
Lernmodus lernen darf — es lässt für sich genommen nichts durch.

⚠️ WARUM TAGESZÄHLER UND NICHT VIER LAUFENDE SUMMEN
---------------------------------------------------
Gefragt sind die Mengen der letzten 30/90/180/360 Tage. Vier mitlaufende Zähler
kann man nicht ehrlich führen: Ein 30-Tage-Zähler müsste wissen, was vor 31
Tagen abzuziehen ist, und das weiss er ohne Zeitreihe nicht. Wer es trotzdem
tut, bekommt Zahlen, die nur monoton steigen und irgendwann alles behaupten.

Eine Zeile je Gerät und Tag ist stattdessen genau — und winzig: 50 Geräte über
360 Tage sind 18.000 Zeilen, die jede Fensterlänge exakt beantworten, auch
solche, die heute noch niemand angefragt hat.

⚠️ KEIN DNS IM MAILPFAD
-----------------------
`gethostbyaddr()` blockiert, und zwar bis zu mehreren Sekunden, wenn der
Namensdienst nicht antwortet. Im Zustellweg wäre das eine Bremse, die genau
dann greift, wenn ohnehin etwas nicht stimmt. `lerne()` legt deshalb ohne Namen
an; aufgelöst wird beim Blick in die Tabelle (`namen_nachtragen`), wo Warten
nicht schadet.
"""
from __future__ import annotations

import logging
import socket
import sqlite3
import secure_io
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

log = logging.getLogger(__name__)

DB_PATH = Path(config.DATA_DIR) / "relay_hosts.db"

# Wie lange Tageszähler vorgehalten werden. Etwas mehr als das grösste Fenster
# (360), damit die 360-Tage-Zahl am Rand nicht schon abgeschnitten ist.
AUFBEWAHRUNG_TAGE = 400

# Die Fenster, die die Übersicht zeigt.
FENSTER = (30, 90, 180, 360)

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    # SQLite legt die Datei mit der umask des Prozesses an (im Container 644).
    # `harden_tree()` beim Start räumt das auf — eine zur Laufzeit ENTSTEHENDE
    # Datenbank bliebe bis zum nächsten Neustart mitlesbar.
    secure_io.harden_file(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS hosts (
        ip              TEXT PRIMARY KEY,
        dns             TEXT NOT NULL DEFAULT '',
        kommentar       TEXT NOT NULL DEFAULT '',
        ansprechpartner TEXT NOT NULL DEFAULT '',
        extern          INTEGER NOT NULL DEFAULT 0,
        gesperrt        INTEGER NOT NULL DEFAULT 0,
        gelernt         INTEGER NOT NULL DEFAULT 0,
        erstellt        TEXT NOT NULL DEFAULT '',
        letzte_mail     TEXT NOT NULL DEFAULT '',
        -- -1 = noch nie geliefert, 0 = zuletzt im Klartext, 1 = zuletzt mit TLS.
        -- Drei Zustaende, weil "noch nie" und "unverschluesselt" verschiedene
        -- Dinge sind: Das eine ist ein neuer Eintrag, das andere ein Befund.
        letzte_tls      INTEGER NOT NULL DEFAULT -1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS tage (
        ip     TEXT NOT NULL,
        tag    TEXT NOT NULL,
        anzahl INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (ip, tag)
    ) WITHOUT ROWID""")
    # Wer hat angeklopft, ohne eingetragen zu sein? Ohne diese Liste bliebe der
    # häufigste Fall im Alltag unsichtbar: ein neues Gerät, das niemand
    # angemeldet hat, dessen Post aber erwartet wird. Der Betreiber sähe nur,
    # dass „nichts ankommt", und suchte am falschen Ende.
    c.execute("""CREATE TABLE IF NOT EXISTS abgewiesen (
        ip        TEXT PRIMARY KEY,
        absender  TEXT NOT NULL DEFAULT '',
        empfaenger TEXT NOT NULL DEFAULT '',
        grund     TEXT NOT NULL DEFAULT '',
        erstmals  TEXT NOT NULL DEFAULT '',
        zuletzt   TEXT NOT NULL DEFAULT '',
        anzahl    INTEGER NOT NULL DEFAULT 0
    )""")
    # Nachrüstung für Datenbanken aus v1.8.4-Vorstufen. `ALTER TABLE ADD
    # COLUMN` ist in SQLite billig und idempotent zu machen — die Alternative
    # (Tabelle neu bauen) verlöre die Einträge, die gerade erst entstanden sind.
    vorhanden = {z["name"] for z in c.execute("PRAGMA table_info(hosts)")}
    if "gesperrt" not in vorhanden:
        c.execute("ALTER TABLE hosts ADD COLUMN gesperrt INTEGER NOT NULL DEFAULT 0")
        log.info("relay_hosts: Spalte 'gesperrt' nachgetragen")
    if "letzte_tls" not in vorhanden:
        c.execute("ALTER TABLE hosts ADD COLUMN letzte_tls INTEGER NOT NULL DEFAULT -1")
        log.info("relay_hosts: Spalte 'letzte_tls' nachgetragen")
    return c


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


def _heute() -> str:
    return _jetzt().strftime("%Y-%m-%d")


def host(ip: str) -> dict | None:
    """Der Eintrag zu dieser Adresse — oder None, wenn sie nicht freigegeben ist."""
    ip = (ip or "").strip()
    if not ip:
        return None
    try:
        with _conn() as c:
            zeile = c.execute("SELECT * FROM hosts WHERE ip=?", (ip,)).fetchone()
        return dict(zeile) if zeile else None
    except Exception as exc:                          # pragma: no cover
        # ⚠️ Bei einem Datenbankfehler wird NICHTS freigegeben. Das ist die
        # umgekehrte Richtung als beim regulären Mailfluss und hier richtig:
        # Ein Relay, das bei einer defekten Datei alles durchlässt, ist ein
        # offenes Relay.
        log.warning("relay_hosts.host(%s) fehlgeschlagen: %s", ip, exc)
        return None


def merke_zustellung(ip: str, tls: bool | None = None) -> None:
    """Dieses Gerät hat gerade eingeliefert — Zeitpunkt und Tageszähler fortschreiben.

    `tls` hält fest, ob die Verbindung verschlüsselt war. ⚠️ Ohne diese Angabe
    wäre nach der Lockerung der STARTTLS-Pflicht (siehe `main._LenientSMTP`)
    nicht mehr erkennbar, welches Gerät im Klartext liefert — und genau das ist
    die Frage, die man beim Austausch eines alten Druckers stellen will.
    """
    ip = (ip or "").strip()
    if not ip:
        return
    try:
        with _lock, _conn() as c:
            if tls is None:
                c.execute("UPDATE hosts SET letzte_mail=? WHERE ip=?",
                          (_jetzt().strftime("%Y-%m-%dT%H:%M:%SZ"), ip))
            else:
                c.execute("UPDATE hosts SET letzte_mail=?, letzte_tls=? WHERE ip=?",
                          (_jetzt().strftime("%Y-%m-%dT%H:%M:%SZ"),
                           1 if tls else 0, ip))
            c.execute("INSERT INTO tage (ip, tag, anzahl) VALUES (?,?,1) "
                      "ON CONFLICT(ip, tag) DO UPDATE SET anzahl = anzahl + 1",
                      (ip, _heute()))
    except Exception as exc:                          # pragma: no cover
        # Zählen darf den Mailfluss nie aufhalten — eine fehlende Zahl ist
        # ärgerlich, eine nicht zugestellte Nachricht ist ein Ausfall.
        log.warning("relay_hosts.merke_zustellung(%s) fehlgeschlagen: %s", ip, exc)


def lerne(ip: str, extern: bool = False) -> bool:
    """Im Lernmodus gesehenes Gerät eintragen. True, wenn es neu war.

    Ohne Namensauflösung — siehe Modulkopf.
    """
    ip = (ip or "").strip()
    if not ip:
        return False
    try:
        with _lock, _conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO hosts (ip, extern, gelernt, erstellt) "
                "VALUES (?,?,1,?)",
                (ip, 1 if extern else 0, _jetzt().strftime("%Y-%m-%dT%H:%M:%SZ")))
            neu = cur.rowcount > 0
        if neu:
            log.info("SMTP-Relay Lernmodus: %s neu aufgenommen", ip)
        return neu
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_hosts.lerne(%s) fehlgeschlagen: %s", ip, exc)
        return False


def _zaehler(c: sqlite3.Connection) -> dict[str, dict[int, int]]:
    """Je Gerät die Summen der vier Fenster — in einer Abfrage.

    Je Fenster einzeln zu fragen wäre lesbarer, ergäbe aber vier Durchläufe
    über dieselben Zeilen; bei einer Tabelle, die jede Übersicht anfasst,
    lohnt das CASE.
    """
    grenzen = {t: (_jetzt() - timedelta(days=t)).strftime("%Y-%m-%d")
               for t in FENSTER}
    aus: dict[str, dict[int, int]] = {}
    spalten = ", ".join(
        f"SUM(CASE WHEN tag >= '{grenzen[t]}' THEN anzahl ELSE 0 END) AS f{t}"
        for t in FENSTER)
    for z in c.execute(f"SELECT ip, {spalten} FROM tage GROUP BY ip"):
        aus[z["ip"]] = {t: z[f"f{t}"] or 0 for t in FENSTER}
    return aus


def liste() -> list[dict]:
    """Alle Geräte mit ihren Zählern, jüngste Einlieferung zuerst."""
    try:
        with _conn() as c:
            zeilen = [dict(z) for z in c.execute("SELECT * FROM hosts")]
            zaehler = _zaehler(c)
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_hosts.liste fehlgeschlagen: %s", exc)
        return []
    for z in zeilen:
        z["extern"] = bool(z["extern"])
        z["gesperrt"] = bool(z["gesperrt"])
        z["tls"] = {1: "ja", 0: "nein"}.get(z.get("letzte_tls"), "unbekannt")
        z["gelernt"] = bool(z["gelernt"])
        z["zaehler"] = zaehler.get(z["ip"], {t: 0 for t in FENSTER})
    zeilen.sort(key=lambda z: z["letzte_mail"] or "", reverse=True)
    return zeilen


def speichern(ip: str, *, dns: str | None = None, kommentar: str | None = None,
              ansprechpartner: str | None = None, extern: bool | None = None,
              gesperrt: bool | None = None) -> bool:
    """Eintrag anlegen oder ändern. Nur übergebene Felder werden geschrieben.

    ⚠️ `None` heisst „unverändert", nicht „leeren". Sonst löschte ein Formular,
    das nur die Freigabe umschaltet, nebenbei den Kommentar.
    """
    ip = (ip or "").strip()
    if not ip:
        return False
    felder = {"dns": dns, "kommentar": kommentar,
              "ansprechpartner": ansprechpartner,
              "extern": None if extern is None else int(bool(extern)),
              "gesperrt": None if gesperrt is None else int(bool(gesperrt))}
    gesetzt = {k: v for k, v in felder.items() if v is not None}
    try:
        with _lock, _conn() as c:
            c.execute("INSERT OR IGNORE INTO hosts (ip, erstellt) VALUES (?,?)",
                      (ip, _jetzt().strftime("%Y-%m-%dT%H:%M:%SZ")))
            if gesetzt:
                satz = ", ".join(f"{k}=?" for k in gesetzt)
                c.execute(f"UPDATE hosts SET {satz} WHERE ip=?",
                          (*gesetzt.values(), ip))
        return True
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_hosts.speichern(%s) fehlgeschlagen: %s", ip, exc)
        return False


def entfernen(ip: str) -> bool:
    """Gerät streichen — samt seiner Zähler."""
    ip = (ip or "").strip()
    if not ip:
        return False
    try:
        with _lock, _conn() as c:
            c.execute("DELETE FROM tage WHERE ip=?", (ip,))
            weg = c.execute("DELETE FROM hosts WHERE ip=?", (ip,)).rowcount
        if weg:
            log.info("SMTP-Relay: %s aus der Geräteliste entfernt", ip)
        return bool(weg)
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_hosts.entfernen(%s) fehlgeschlagen: %s", ip, exc)
        return False


def merke_abweisung(ip: str, absender: str = "", empfaenger: list | None = None,
                    grund: str = "") -> None:
    """Ein nicht eingetragenes Gerät hat angeklopft.

    Eine Zeile je Adresse, kein Verlauf: Gefragt ist „wer will hier senden",
    nicht „wie oft hat es ein Bot versucht". Ein Zähler und der letzte
    Zeitpunkt genügen, und die Tabelle kann nicht volllaufen.
    """
    ip = (ip or "").strip()
    if not ip:
        return
    jetzt = _jetzt().strftime("%Y-%m-%dT%H:%M:%SZ")
    ziel = ", ".join((empfaenger or [])[:3])
    try:
        with _lock, _conn() as c:
            c.execute(
                "INSERT INTO abgewiesen (ip, absender, empfaenger, grund, "
                "erstmals, zuletzt, anzahl) VALUES (?,?,?,?,?,?,1) "
                "ON CONFLICT(ip) DO UPDATE SET absender=excluded.absender, "
                "empfaenger=excluded.empfaenger, grund=excluded.grund, "
                "zuletzt=excluded.zuletzt, anzahl = anzahl + 1",
                (ip, (absender or "")[:200], ziel[:300], (grund or "")[:300],
                 jetzt, jetzt))
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_hosts.merke_abweisung(%s) fehlgeschlagen: %s", ip, exc)


def abgewiesene(grenze: int = 100) -> list[dict]:
    """Wer wurde zuletzt abgewiesen? Jüngste zuerst."""
    try:
        with _conn() as c:
            return [dict(z) for z in c.execute(
                "SELECT * FROM abgewiesen ORDER BY zuletzt DESC LIMIT ?",
                (grenze,))]
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_hosts.abgewiesene fehlgeschlagen: %s", exc)
        return []


def vergiss_abweisung(ip: str) -> bool:
    """Eintrag aus der Abweisungsliste streichen (übernommen oder uninteressant)."""
    try:
        with _lock, _conn() as c:
            return bool(c.execute("DELETE FROM abgewiesen WHERE ip=?",
                                  ((ip or "").strip(),)).rowcount)
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_hosts.vergiss_abweisung(%s) fehlgeschlagen: %s", ip, exc)
        return False


def abweisungen_leeren() -> int:
    try:
        with _lock, _conn() as c:
            return c.execute("DELETE FROM abgewiesen").rowcount
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_hosts.abweisungen_leeren fehlgeschlagen: %s", exc)
        return 0


def namen_nachtragen(zeitlimit: float = 1.0) -> int:
    """Fehlende Rückwärtsauflösungen ergänzen. Gibt die Zahl der Treffer zurück.

    Wird beim Blick in die Übersicht gerufen, nicht im Mailpfad. Ein Gerät ohne
    `PTR` bekommt einen Strich eingetragen, damit nicht bei jedem Aufruf erneut
    auf den Namensdienst gewartet wird.
    """
    try:
        with _conn() as c:
            offen = [z["ip"] for z in
                     c.execute("SELECT ip FROM hosts WHERE dns=''")]
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_hosts.namen_nachtragen fehlgeschlagen: %s", exc)
        return 0

    gefunden = 0
    alt = socket.getdefaulttimeout()
    for ip in offen:
        name = "—"
        try:
            socket.setdefaulttimeout(zeitlimit)
            name = socket.gethostbyaddr(ip)[0] or "—"
            gefunden += 1
        except Exception:                             # noqa: BLE001
            pass                                      # kein PTR — Strich bleibt
        finally:
            socket.setdefaulttimeout(alt)
        speichern(ip, dns=name)
    return gefunden


def aufraeumen(tage: int = AUFBEWAHRUNG_TAGE) -> int:
    """Tageszähler jenseits der Aufbewahrung löschen. Geräte bleiben."""
    grenze = (_jetzt() - timedelta(days=tage)).strftime("%Y-%m-%d")
    try:
        with _lock, _conn() as c:
            return c.execute("DELETE FROM tage WHERE tag < ?", (grenze,)).rowcount
    except Exception as exc:                          # pragma: no cover
        log.warning("relay_hosts.aufraeumen fehlgeschlagen: %s", exc)
        return 0
