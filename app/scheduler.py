"""Wiederkehrende Arbeiten — ein Faden, zwei Takte.

Stündlich: Postfachliste aus Exchange Online nachladen (wenn eingeschaltet),
           TLS-Zertifikat prüfen und über Let's Encrypt erneuern (`_tls_pruefen`,
           höchstens ein Versuch je Tag).
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
    # Zuerst der Schlüssel: Läuft er ab, scheitert danach auch die Postfachabfrage.
    # Ohne Netz, solange er nicht fällig ist (Lesen der PFX genügt).
    # Eigener try: Ein Fehler hier darf die Postfachabfrage nicht mitnehmen.
    import auth_cert
    try:
        auth_cert.erneuern()
    except Exception as exc:                                  # noqa: BLE001
        log.error("Erneuerung des Auth-Zertifikats: %s", exc)
    try:
        _tls_pruefen()
    except Exception as exc:                                  # noqa: BLE001
        log.error("Prüfung des TLS-Zertifikats: %s", exc)
    # Nach dem Herabstufen: messen, bis Exchange es übernommen hat (rechte.py).
    import rechte
    try:
        rechte.nachmessen()
    except Exception as exc:                                  # noqa: BLE001
        log.error("Nachmessen der App-Rechte: %s", exc)
    import exo_mailboxes
    import settings_store
    if settings_store.get("EXO_ABFRAGE_AN"):
        vorher = exo_mailboxes.zustand()["postfaecher"]
        liste = exo_mailboxes.list_mailboxes(force=True)
        log.info("Postfachliste nachgeladen: %d Postfächer (vorher %d)", len(liste), vorher)


_le_versuch_tag = ""


def _tls_pruefen() -> str:
    """TLS-Zertifikat kurz vor Ablauf über Let's Encrypt erneuern (wie Gateway
    `_check_tls_cert`/`_try_le_renewal`). Rückgabe: was geschah (für Tests).

    Stündlich statt täglich: Der Tagestakt beginnt erst 24 h nach dem Start —
    ein Relay, das öfter neu startet, käme nie dazu. Versucht wird trotzdem
    höchstens einmal je Tag.

    Erneuert wird über die gemeinsame Quelle `le_certbot` (dieselben
    Verzeichnisse und dieselbe Übernahme wie die Ausstellung). Fehlt certbots
    Konfiguration, obwohl zuletzt über HTTP-01 ausgestellt wurde — so nach einer
    Wiederherstellung aus dem Backup, das `le-config/` nicht enthält
    (backup_manager) —, wird mit den gespeicherten Angaben neu ausgestellt.
    Ein DNS-01- oder PFX-Zertifikat erneuert niemand selbst; dafür steht das
    Band „TLS-Zertifikat läuft ab“ auf jeder Seite (webui/deps.py).
    Einen Neustart braucht es nicht: der Listener lädt das neue Zertifikat bei
    der nächsten Verbindung."""
    global _le_versuch_tag
    import datetime as _dt
    import le_certbot
    import settings_store
    import tls_cert
    st = tls_cert.info()
    if not st.get("vorhanden") or st.get("fehler") or st.get("selbstsigniert"):
        return "kein-le"
    tage = int(st.get("tage_rest", 9999))
    if tage > int(settings_store.get("LE_RENEW_DAYS") or 14):
        return "nicht-faellig"
    if settings_store.get("LE_AUTO_RENEW") is False:
        log.warning("TLS-Zertifikat läuft in %d Tagen ab — automatische Erneuerung ist aus", tage)
        return "aus"
    heute = _dt.date.today().isoformat()
    if _le_versuch_tag == heute:
        return "heute-schon"
    _le_versuch_tag = heute

    log.info("TLS-Zertifikat läuft in %d Tagen ab — Erneuerung über certbot", tage)
    status, info = le_certbot.erneuern()
    if status == "not_managed":
        lauf = settings_store.get("LE_LAUF") or {}
        domain = (settings_store.get("LE_DOMAIN") or settings_store.get("PUBLIC_HOSTNAME") or "").strip()
        if lauf.get("weg") == "http01" and domain and st.get("lets_encrypt") \
                and domain in (st.get("hostnames") or []):
            ok, info = le_certbot.ausstellen(domain, (settings_store.get("LE_EMAIL") or "").strip())
            status = "renewed" if ok else "error"
        else:
            log.warning("TLS-Zertifikat läuft in %d Tagen ab und wird nicht automatisch "
                        "erneuert (kein HTTP-01 über certbot) — bitte selbst erneuern", tage)
            return "manuell"
    if status == "renewed":
        log.info("TLS-Zertifikat erneuert, gültig bis %s — wird ab der nächsten Verbindung genutzt", info)
        le_certbot.lauf_vermerken(True, f"Erneuert, gültig bis {info}", "http01")
    elif status == "error":
        log.error("Erneuerung des TLS-Zertifikats fehlgeschlagen: %s", info)
        le_certbot.lauf_vermerken(False, f"Erneuerung fehlgeschlagen: {info}", "http01")
    else:
        log.warning("certbot hat nichts erneuert (%s) — Ablauf in %d Tagen", status, tage)
    return status


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
