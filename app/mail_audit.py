"""Mail-Protokoll je Einlieferung — SQLite im Datenverzeichnis.

Eine Zeile je SMTP-Transaktion: angenommen, abgelehnt, Zustellfehler. Das ist
die Stelle, an der ein Betreiber nachschlägt, warum ein Drucker seit Tagen
nichts mehr zustellt — das Logbuch rotiert, diese Tabelle bleibt.

Schmale Fassung des gleichnamigen Gateway-Moduls: dieselben Spalten und
dieselbe `log_event()`-Signatur, ohne Graph-Zähler und Stundenstatistik.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import secure_io

log = logging.getLogger(__name__)

DB_PATH = Path(config.DATA_DIR) / "mail_audit.db"
_lock = threading.Lock()
_initialised = False


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    secure_io.harden_file(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    global _initialised
    secure_io.ensure_dir(DB_PATH.parent)
    with _lock, _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT    NOT NULL,
                sender        TEXT,
                recipients    TEXT,
                subject       TEXT,
                message_id    TEXT,
                action        TEXT,
                size_bytes    INTEGER,
                processing_ms INTEGER,
                error         TEXT,
                quelle        TEXT,
                -- 1 = mit STARTTLS, 0 = Klartext; 1 = mindestens ein Ziel ausserhalb
                tls           INTEGER NOT NULL DEFAULT 0,
                extern        INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts     ON mail_log(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_action ON mail_log(action)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_quelle ON mail_log(quelle)")
        vorhanden = {z["name"] for z in conn.execute("PRAGMA table_info(mail_log)")}
        for spalte in ("tls", "extern"):
            if spalte not in vorhanden:
                conn.execute(f"ALTER TABLE mail_log ADD COLUMN {spalte} INTEGER NOT NULL DEFAULT 0")
    _initialised = True
    log.info("mail_audit: Datenbank bereit unter %s", DB_PATH)


def log_event(*, sender: str, recipients: list[str], subject: str, message_id: str,
              action: str, size_bytes: int = 0, processing_ms: int = 0,
              error: str | None = None, quelle: str = "", tls: bool = False,
              extern: bool = False) -> None:
    if not _initialised:
        log.warning("mail_audit: log_event(%s) verworfen — Datenbank nicht bereit", action)
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _lock, _conn() as conn:
            conn.execute(
                "INSERT INTO mail_log (ts, sender, recipients, subject, message_id, action, "
                "size_bytes, processing_ms, error, quelle, tls, extern) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, sender, json.dumps(recipients, ensure_ascii=False), subject, message_id,
                 action, size_bytes, processing_ms, error, quelle, int(bool(tls)), int(bool(extern))))
    except Exception as exc:                                  # noqa: BLE001
        log.warning("mail_audit: Schreiben fehlgeschlagen: %s", exc)


def query_events(*, action: str | None = None, quelle: str | None = None,
                 limit: int = 200, offset: int = 0) -> list[dict]:
    if not _initialised:
        return []
    bedingungen, params = [], []
    if action:
        bedingungen.append("action = ?")
        params.append(action)
    if quelle:
        bedingungen.append("quelle = ?")
        params.append(quelle)
    where = ("WHERE " + " AND ".join(bedingungen)) if bedingungen else ""
    try:
        with _conn() as conn:
            zeilen = conn.execute(
                f"SELECT * FROM mail_log {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
                (*params, limit, offset)).fetchall()
        aus = []
        for z in zeilen:
            d = dict(z)
            try:
                d["recipients"] = json.loads(d.get("recipients") or "[]")
            except Exception:                                 # noqa: BLE001
                d["recipients"] = []
            aus.append(d)
        return aus
    except Exception as exc:                                  # noqa: BLE001
        log.warning("mail_audit: Abfrage fehlgeschlagen: %s", exc)
        return []


def zaehler_heute() -> dict[str, int]:
    """Je Aktion die Zahl der Ereignisse des laufenden Tages (UTC)."""
    if not _initialised:
        return {}
    heute = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with _conn() as conn:
            zeilen = conn.execute(
                "SELECT action, COUNT(*) AS n FROM mail_log WHERE ts >= ? GROUP BY action",
                (f"{heute}T00:00:00Z",)).fetchall()
        return {z["action"]: z["n"] for z in zeilen}
    except Exception as exc:                                  # noqa: BLE001
        log.warning("mail_audit: Tageszähler fehlgeschlagen: %s", exc)
        return {}


def auswertung(tage: int = 30) -> dict:
    """Für das Dashboard: je Gerät, was in den letzten `tage` Tagen geschah.

    Liefert {"quellen": {ip: {...}}, "gesamt": {...}} mit je: zugestellt, tls,
    klartext, intern, extern, abgelehnt, fehler. Die Zahlen kommen aus dem
    Mail-Protokoll, nicht aus den Tageszählern der Geräteliste: Nur hier steht,
    OB eine Einlieferung verschlüsselt war und WOHIN sie ging.
    """
    leer = lambda: {"zugestellt": 0, "tls": 0, "klartext": 0, "intern": 0, "extern": 0,  # noqa: E731
                    "abgelehnt": 0, "fehler": 0}
    if not _initialised:
        return {"quellen": {}, "gesamt": leer()}
    grenze = (datetime.now(timezone.utc) - timedelta(days=tage)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _conn() as conn:
            zeilen = conn.execute(
                "SELECT quelle, "
                "SUM(CASE WHEN action='relay' THEN 1 ELSE 0 END) AS zugestellt, "
                "SUM(CASE WHEN action='relay' AND tls=1 THEN 1 ELSE 0 END) AS tls, "
                "SUM(CASE WHEN action='relay' AND tls=0 THEN 1 ELSE 0 END) AS klartext, "
                "SUM(CASE WHEN action='relay' AND extern=0 THEN 1 ELSE 0 END) AS intern, "
                "SUM(CASE WHEN action='relay' AND extern=1 THEN 1 ELSE 0 END) AS extern, "
                "SUM(CASE WHEN action='relay_abgelehnt' THEN 1 ELSE 0 END) AS abgelehnt, "
                "SUM(CASE WHEN action='relay_fehler' THEN 1 ELSE 0 END) AS fehler "
                "FROM mail_log WHERE ts >= ? GROUP BY quelle", (grenze,)).fetchall()
    except Exception as exc:                                  # noqa: BLE001
        log.warning("mail_audit: Auswertung fehlgeschlagen: %s", exc)
        return {"quellen": {}, "gesamt": leer()}
    quellen: dict[str, dict] = {}
    gesamt = leer()
    for z in zeilen:
        d = {k: int(z[k] or 0) for k in gesamt}
        quellen[z["quelle"] or "?"] = d
        for k in gesamt:
            gesamt[k] += d[k]
    return {"quellen": quellen, "gesamt": gesamt, "tage": tage}


def prune_old_events(retention_days: int = 90) -> int:
    if not _initialised:
        return 0
    grenze = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _lock, _conn() as conn:
            weg = conn.execute("DELETE FROM mail_log WHERE ts < ?", (grenze,)).rowcount
        if weg:
            log.info("mail_audit: %d Einträge älter als %d Tage entfernt", weg, retention_days)
        return weg
    except Exception as exc:                                  # noqa: BLE001
        log.warning("mail_audit: Aufräumen fehlgeschlagen: %s", exc)
        return 0
