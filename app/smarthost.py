"""Der Rückweg: die angenommene Nachricht an Exchange Online übergeben.

ZWEI WEGE
---------
`smarthost` (Vorgabe) — SMTP mit STARTTLS an `<tenant>.mail.protection.outlook.com`
    auf Port 25. Der Inbound-Connector auf EXO-Seite erkennt den Dienst am
    Zertifikatsnamen (`TlsSenderCertificateName`) und nimmt Post für beliebige
    Absender der eigenen Domänen an — genau das braucht ein Relay, dessen
    Drucker als `scanner@firma.de` senden. Braucht ausgehenden Port 25.

`submit` — SMTP AUTH auf Port 587 (`smtp.office365.com`) mit einem Dienstkonto.
    Für Standorte ohne ausgehenden Port 25. Zwei Authentifizierungswege:

    1. OAuth2 mit SMTP.SendAsApp (empfohlen) — erfordert:
       - SUBMIT_CLIENT_ID + SUBMIT_CLIENT_SECRET (dedizierte App-Registrierung)
       - Ermöglicht "Send As" für mehrere Absenderadressen

    2. Basic Auth (Fallback) — SUBMIT_USER + SUBMIT_PASSWORD:
       - Einfach einzurichten, aber Exchange schreibt den Absender um,
         sofern dem Konto kein „Senden als" erteilt ist
       - SMTP AUTH muss am Konto aktiviert sein

Beide Wege werfen bei einem Fehler. Der Handler übersetzt das in `451`, damit
das Gerät es erneut versucht — die Nachricht ist dann NICHT angenommen.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from pathlib import Path

import msal

import config
import settings_store

log = logging.getLogger(__name__)

_SMTP_SCOPE = ["https://outlook.office365.com/.default"]


def _acquire_oauth_token() -> str | None:
    """OAuth2 Token für SMTP XOAUTH2 mit SMTP.SendAsApp.

    Nutzt dedizierte Credentials (SUBMIT_CLIENT_ID/SECRET).
    Beide sind erforderlich — der Relay hat keine Main-App wie das Gateway.
    """
    client_id = (settings_store.get("SUBMIT_CLIENT_ID") or "").strip()
    client_secret = (settings_store.get("SUBMIT_CLIENT_SECRET") or "").strip()
    tenant = settings_store.get("TENANT_ID") or ""

    if not (client_id and client_secret and tenant):
        return None

    try:
        authority = f"https://login.microsoftonline.com/{tenant}"
        app = msal.ConfidentialClientApplication(
            client_id, authority=authority, client_credential=client_secret)
        result = app.acquire_token_for_client(scopes=_SMTP_SCOPE)
        if result and "access_token" in result:
            log.debug("OAuth2 Token erfolgreich erworben")
            return result["access_token"]
        log.debug("OAuth2 Token-Erwerb fehlgeschlagen: %s",
                  result.get("error_description") if result else "no result")
    except Exception as exc:  # noqa: BLE001
        log.debug("OAuth2 Token-Erwerb Exception: %s", exc)

    return None


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

    # Versuche zuerst OAuth2 (SMTP.SendAsApp)
    token = _acquire_oauth_token()
    if token:
        try:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                auth_string = f"user=\r\nauth=Bearer {token}\r\n\r\n"
                smtp.auth("XOAUTH2", lambda _: auth_string.encode("utf-8"))
                smtp.sendmail(mail_from, rcpt_tos, content)
            return f"{host}:{port} via XOAUTH2"
        except smtplib.SMTPAuthenticationError:
            log.warning("SMTP XOAUTH2 auth fehlgeschlagen, versuche Basic Auth")
        except Exception as exc:  # noqa: BLE001
            log.warning("SMTP XOAUTH2 fehlgeschlagen: %s, versuche Basic Auth", exc)

    # Fallback auf Basic Auth
    user = (settings_store.get("SUBMIT_USER") or "").strip()
    pw = settings_store.get("SUBMIT_PASSWORD") or ""
    if not (user and pw):
        raise RuntimeError("SUBMIT_USER/SUBMIT_PASSWORD sind nicht konfiguriert und OAuth2 nicht verfügbar")

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

            # Versuche OAuth2
            token = _acquire_oauth_token()
            if token:
                try:
                    with smtplib.SMTP(host, port, timeout=20) as smtp:
                        smtp.ehlo()
                        smtp.starttls(context=ssl.create_default_context())
                        smtp.ehlo()
                        auth_string = f"user=\r\nauth=Bearer {token}\r\n\r\n"
                        smtp.auth("XOAUTH2", lambda _: auth_string.encode("utf-8"))
                    return {"ok": True, "text": f"{host}:{port} — STARTTLS und XOAUTH2 erfolgreich"}
                except smtplib.SMTPAuthenticationError:
                    log.debug("XOAUTH2 auth fehlgeschlagen, versuche Basic Auth")
                except Exception as exc:  # noqa: BLE001
                    log.debug("XOAUTH2 fehlgeschlagen: %s", exc)

            # Fallback Basic Auth
            user = (settings_store.get("SUBMIT_USER") or "").strip()
            pw = settings_store.get("SUBMIT_PASSWORD") or ""
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                if user and pw:
                    smtp.login(user, pw)
                    return {"ok": True, "text": f"{host}:{port} — STARTTLS und Basic Auth erfolgreich"}
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
