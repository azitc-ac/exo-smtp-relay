"""Der Rückweg: die angenommene Nachricht an Exchange Online übergeben.

ZWEI WEGE
---------
`smarthost` (Vorgabe) — SMTP mit STARTTLS an `<tenant>.mail.protection.outlook.com`
    auf Port 25. Der Inbound-Connector auf EXO-Seite erkennt den Dienst am
    Zertifikatsnamen (`TlsSenderCertificateName`) und nimmt Post für beliebige
    Absender der eigenen Domänen an — genau das braucht ein Relay, dessen
    Drucker als `scanner@firma.de` senden. Braucht ausgehenden Port 25.

`submit` — SMTP AUTH auf Port 587 (`smtp.office365.com`) mit einem Dienstkonto.
    Für Standorte ohne ausgehenden Port 25. ⚠️ Exchange schreibt den Absender
    auf das Dienstkonto um, sofern diesem kein „Senden als" für die jeweilige
    Adresse erteilt ist; und SMTP AUTH muss am Konto freigeschaltet sein. Der
    Weg ist die Ausnahme, nicht die Regel — deshalb nicht Vorgabe.

Beide Wege werfen bei einem Fehler. Der Handler übersetzt das in `451`, damit
das Gerät es erneut versucht — die Nachricht ist dann NICHT angenommen, und
das ist richtig: Ein Relay, das „250 OK" sagt und die Post verliert, ist der
schlechteste aller Ausgänge.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from pathlib import Path

import config
import settings_store

log = logging.getLogger(__name__)


def _client_tls_context() -> ssl.SSLContext:
    """STARTTLS-Kontext mit dem eigenen Zertifikat als Client-Zertifikat —
    darüber erkennt der Inbound-Connector den Dienst."""
    ctx = ssl.create_default_context()
    cert, key = Path(config.SMTP_TLS_CERT), Path(config.SMTP_TLS_KEY)
    if cert.exists() and key.exists():
        try:
            ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        except Exception as exc:                              # noqa: BLE001
            log.debug("Client-Zertifikat für STARTTLS nicht ladbar: %s", exc)
    return ctx


def _send_smarthost(mail_from: str, rcpt_tos: list[str], content: bytes) -> str:
    host = (settings_store.get("EXO_SMARTHOST") or "").strip()
    port = int(settings_store.get("EXO_PORT") or 25)
    if not host:
        raise RuntimeError("EXO_SMARTHOST ist nicht konfiguriert")
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls(context=_client_tls_context())
        smtp.ehlo()
        smtp.sendmail(mail_from, rcpt_tos, content)
    return f"{host}:{port}"


def _send_submit(mail_from: str, rcpt_tos: list[str], content: bytes) -> str:
    host = (settings_store.get("SUBMIT_HOST") or "smtp.office365.com").strip()
    port = int(settings_store.get("SUBMIT_PORT") or 587)
    user = (settings_store.get("SUBMIT_USER") or "").strip()
    pw = settings_store.get("SUBMIT_PASSWORD") or ""
    if not (user and pw):
        raise RuntimeError("SUBMIT_USER/SUBMIT_PASSWORD sind nicht konfiguriert")
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(user, pw)
        smtp.sendmail(mail_from, rcpt_tos, content)
    return f"{host}:{port} als {user}"


def send(mail_from: str, rcpt_tos: list[str], content: bytes) -> None:
    """Nachricht unverändert weiterreichen. Wirft bei jedem Fehler."""
    modus = (settings_store.get("EXO_SUBMIT_MODE") or "smarthost").strip()
    try:
        if modus == "submit":
            ziel = _send_submit(mail_from, rcpt_tos, content)
        else:
            ziel = _send_smarthost(mail_from, rcpt_tos, content)
        log.info("Zustellung OK: from=%s to=%s via %s", mail_from, rcpt_tos, ziel)
    except Exception as exc:
        log.error("Zustellung fehlgeschlagen: from=%s to=%s: %s", mail_from, rcpt_tos, exc)
        raise


def verbindungstest() -> dict:
    """Nur EHLO + STARTTLS (+ Anmeldung im Modus submit), keine Nachricht."""
    modus = (settings_store.get("EXO_SUBMIT_MODE") or "smarthost").strip()
    try:
        if modus == "submit":
            host = (settings_store.get("SUBMIT_HOST") or "smtp.office365.com").strip()
            port = int(settings_store.get("SUBMIT_PORT") or 587)
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                user = (settings_store.get("SUBMIT_USER") or "").strip()
                pw = settings_store.get("SUBMIT_PASSWORD") or ""
                if user and pw:
                    smtp.login(user, pw)
                    return {"ok": True, "text": f"{host}:{port} — STARTTLS und Anmeldung als {user} erfolgreich"}
                return {"ok": True, "text": f"{host}:{port} — STARTTLS erfolgreich (kein Konto hinterlegt)"}
        host = (settings_store.get("EXO_SMARTHOST") or "").strip()
        port = int(settings_store.get("EXO_PORT") or 25)
        if not host:
            return {"ok": False, "text": "Smarthost ist nicht eingetragen"}
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=_client_tls_context())
            code, antwort = smtp.ehlo()
        return {"ok": True, "text": f"{host}:{port} — STARTTLS erfolgreich ({code} {antwort.decode(errors='replace').splitlines()[0] if antwort else ''})"}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "text": f"{type(exc).__name__}: {exc}"}
