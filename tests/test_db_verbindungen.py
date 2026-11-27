"""Datenbankverbindungen werden nach dem Block GESCHLOSSEN.

⚠️ `with sqlite3.connect(...) as c:` schließt nicht — der Kontextmanager von sqlite3
macht nur commit/rollback. Unter Python 3.13 lief die Testreihe bei `ulimit -n 1024`
(Debian-Vorgabe) in „Too many open files“ (27.11.2026, ~1000 ResourceWarnings
„unclosed database“). Diese Tests scheitern, wenn `_conn()` wieder eine offene
Verbindung zurücklässt.
"""
import sqlite3

import pytest


def test_mail_audit_schliesst_die_verbindung(monkeypatch, tmp_path):
    import mail_audit
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    mail_audit.init_db()
    with mail_audit._conn() as c:
        c.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")


def test_mail_audit_rollt_bei_fehler_zurueck(monkeypatch, tmp_path):
    import mail_audit
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    mail_audit.init_db()
    with pytest.raises(RuntimeError):
        with mail_audit._conn() as c:
            c.execute("CREATE TABLE probe (x INTEGER)")
            c.execute("INSERT INTO probe VALUES (1)")
            raise RuntimeError("abbrechen")
    with mail_audit._conn() as c:
        tabellen = {z[0] for z in c.execute("SELECT name FROM sqlite_master")}
    # CREATE TABLE committet in sqlite3 implizit nicht im Legacy-Modus — die Zeile darf fehlen.
    if "probe" in tabellen:
        with mail_audit._conn() as c:
            assert c.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
