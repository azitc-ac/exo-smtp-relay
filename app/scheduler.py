"""Wiederkehrende Arbeiten — ein Faden, zwei Takte.

Stündlich: Postfachliste aus Exchange Online nachladen (wenn eingeschaltet).
Täglich:   Mail-Protokoll und Tageszähler der Geräte aufräumen.

Bewusst ohne Bibliothek: Zwei Takte rechtfertigen keinen Zeitplaner mit
eigener Konfiguration. `stop()` beendet den Faden beim Herunterfahren.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

_stop = threading.Event()
_thread: threading.Thread | None = None

STUNDE = 3600
TAG = 24 * STUNDE


def _stuendlich() -> None:
    import exo_mailboxes
    import settings_store
    if settings_store.get("EXO_ABFRAGE_AN"):
        vorher = exo_mailboxes.zustand()["postfaecher"]
        liste = exo_mailboxes.list_mailboxes(force=True)
        log.info("Postfachliste nachgeladen: %d Postfächer (vorher %d)", len(liste), vorher)


def _taeglich() -> None:
    import mail_audit
    import relay_hosts
    import settings_store
    mail_audit.prune_old_events(int(settings_store.get("LOG_RETENTION_DAYS") or 90))
    relay_hosts.aufraeumen()


def _lauf() -> None:
    naechste_stunde = 0.0                     # sofort beim Start
    naechster_tag = time.monotonic() + TAG
    while not _stop.is_set():
        jetzt = time.monotonic()
        if jetzt >= naechste_stunde:
            try:
                _stuendlich()
            except Exception as exc:                          # noqa: BLE001
                log.error("Stündlicher Lauf fehlgeschlagen: %s", exc)
            naechste_stunde = jetzt + STUNDE
        if jetzt >= naechster_tag:
            try:
                _taeglich()
            except Exception as exc:                          # noqa: BLE001
                log.error("Täglicher Lauf fehlgeschlagen: %s", exc)
            naechster_tag = jetzt + TAG
        _stop.wait(30)


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_lauf, name="scheduler", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
