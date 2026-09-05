"""Einstieg: SMTP-Listener auf Port 25, Weboberfläche, Zeitplaner.

Läuft als Container-Prozess, systemd-Dienst oder Windows-Dienst (siehe
`windows/service.py`) — überall derselbe Aufruf: `python main.py`.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import ssl
import sys
import threading
from pathlib import Path

import uvicorn
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP as _BaseSMTP, syntax, MISSING

import config
import settings_store

# Vor dem webui-Import: dort hängt sich ein Speicher-Handler an den Root-Logger,
# und basicConfig() ist danach wirkungslos.
logging.basicConfig(
    level=getattr(logging, config._ENV_SEEDS.get("LOG_LEVEL", "INFO"), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

import log_manager                                            # noqa: E402
import scheduler                                              # noqa: E402
import smtp_rauschen                                          # noqa: E402
from handler import RelayHandler                              # noqa: E402

log = logging.getLogger(__name__)


class _LenientSMTP(_BaseSMTP):
    """aiosmtpd.SMTP mit zwei Anpassungen aus dem Gateway.

    1. `connection_made`: STARTTLS bleibt angeboten, wird aber für ein
       freigegebenes Relay-Gerät nicht VERLANGT — ein Etikettendrucker von 2011
       kann es nicht, und genau für solche Geräte gibt es diesen Dienst. Für
       jede andere Adresse bleibt die Pflicht; die Entscheidung fällt bei jeder
       Verbindung neu (Sperre oder Entfernen wirken ohne Neustart).
    2. `smtp_MAIL`: unbekannte MAIL-FROM-Parameter werden übergangen statt mit
       555 abgewiesen. Manche Geräte senden `AUTH=<>` oder Ähnliches mit.
    """

    def connection_made(self, transport) -> None:
        super().connection_made(transport)
        if not self.require_starttls:
            return                       # ohne Zertifikat gibt es keine Pflicht
        try:
            ip = (self.session.peer or ("",))[0]
        except Exception:                # noqa: BLE001
            return                       # im Zweifel bei der Pflicht bleiben
        try:
            import smtp_relay
            if smtp_relay.ist_relay_quelle(ip):
                self.require_starttls = False
                log.info("SMTP: %s ist ein freigegebenes Relay-Gerät — "
                         "STARTTLS wird angeboten, aber nicht verlangt", ip)
        except Exception as exc:         # noqa: BLE001
            log.warning("SMTP: Relay-Prüfung für %s fehlgeschlagen: %s", ip, exc)

    @syntax('MAIL FROM: <address>', extended=' [SP <mail-parameters>]')
    async def smtp_MAIL(self, arg):
        if await self.check_helo_needed():
            return
        if await self.check_auth_needed("MAIL"):
            return
        syntaxerr = '501 Syntax: MAIL FROM: <address>'
        if self.session.extended_smtp:
            syntaxerr += ' [SP <mail-parameters>]'
        if arg is None:
            await self.push(syntaxerr)
            return
        arg = self._strip_command_keyword('FROM:', arg)
        if arg is None:
            await self.push(syntaxerr)
            return
        address, addrparams = self._getaddr(arg)
        if address is None:
            await self.push("553 5.1.3 Error: malformed address")
            return
        if not address:
            await self.push(syntaxerr)
            return
        if not self.session.extended_smtp and addrparams:
            await self.push(syntaxerr)
            return
        if self.envelope.mail_from:
            await self.push('503 Error: nested MAIL command')
            return
        mail_options = addrparams.upper().split()
        params = self._getparams(mail_options)
        if params is None:
            await self.push(syntaxerr)
            return
        if not self._decode_data:
            body = params.pop('BODY', '7BIT')
            if body not in ['7BIT', '8BITMIME']:
                await self.push('501 Error: BODY can only be one of 7BIT, 8BITMIME')
                return
        smtputf8 = params.pop('SMTPUTF8', False)
        if not isinstance(smtputf8, bool):
            await self.push('501 Error: SMTPUTF8 takes no arguments')
            return
        if smtputf8 and not self.enable_SMTPUTF8:
            await self.push('501 Error: SMTPUTF8 disabled')
            return
        self.envelope.smtp_utf8 = smtputf8
        size = params.pop('SIZE', None)
        if size:
            if isinstance(size, bool) or not size.isdigit():
                await self.push(syntaxerr)
                return
            elif self.data_size_limit and int(size) > self.data_size_limit:
                await self.push('552 Error: message size exceeds fixed maximum message size')
                return
        if params:
            log.debug("Unbekannte MAIL-FROM-Parameter von %s übergangen: %s",
                      self.session.peer, list(params.keys()))
        status = await self._call_handler_hook('MAIL', address, mail_options)
        if status is MISSING:
            self.envelope.mail_from = address
            self.envelope.mail_options.extend(mail_options)
            status = '250 OK'
        log.info('%r sender: %s', self.session.peer, address)
        await self.push(status)


class _LenientController(Controller):
    def factory(self):
        return _LenientSMTP(self.handler, **self.SMTP_kwargs)


def _build_tls_context() -> ssl.SSLContext | None:
    cert, key = Path(config.SMTP_TLS_CERT), Path(config.SMTP_TLS_KEY)
    if not cert.exists() or not key.exists():
        log.warning("TLS-Zertifikat fehlt (%s / %s) — SMTP läuft OHNE STARTTLS", cert, key)
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    # Alte Geräte: TLS 1.0/1.1 zulassen. Ein Scanner von 2012 kann nichts
    # Besseres, und Klartext wäre die Alternative — nicht ein neueres TLS.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)   # TLSv1 ist absichtlich
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except (ValueError, AttributeError):
            pass
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return ctx


def _run_webui() -> None:
    from webui.app import app as fastapi_app
    cert, key = Path(config.SMTP_TLS_CERT), Path(config.SMTP_TLS_KEY)
    ssl_kwargs: dict = {}
    if cert.exists() and key.exists():
        ssl_kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        log.info("Web-UI mit TLS (https://%s:%d)", config.WEBUI_BIND, config.WEBUI_PORT)

    class _OhneRauschen(logging.Filter):
        _still = ("/health", "/api/whoami", "/static/", "/favicon")

        def filter(self, satz: logging.LogRecord) -> bool:
            return not any(p in satz.getMessage() for p in self._still)

    logging.getLogger("uvicorn.access").addFilter(_OhneRauschen())
    uvicorn.run(fastapi_app, host=config.WEBUI_BIND, port=config.WEBUI_PORT,
                log_level=(settings_store.get("LOG_LEVEL") or "info").lower(),
                access_log=True, **ssl_kwargs)


_beenden: asyncio.Event | None = None


async def _run_smtp() -> None:
    global _beenden
    _beenden = asyncio.Event()
    tls_ctx = _build_tls_context()
    controller = _LenientController(
        RelayHandler(), hostname=config.SMTP_BIND, port=config.SMTP_PORT,
        tls_context=tls_ctx, require_starttls=tls_ctx is not None,
    )
    controller.start()
    import runtime_state
    runtime_state.smtp_controller = controller
    log.info("SMTP-Listener auf %s:%d gestartet (STARTTLS: %s)",
             config.SMTP_BIND, config.SMTP_PORT, "ja" if tls_ctx else "nein")

    loop = asyncio.get_running_loop()
    # SIGTERM/SIGINT sauber beenden — add_signal_handler gibt es unter Windows
    # nicht, dort beendet der Dienstwrapper den Prozess.
    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _beenden.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await _beenden.wait()
    finally:
        log.info("SMTP-Listener wird beendet")
        controller.stop()
        scheduler.stop()


def main() -> None:
    settings_store.init(config._ENV_SEEDS)
    log_manager.setup(retention_days=int(settings_store.get("LOG_RETENTION_DAYS") or 30),
                      tz_name=settings_store.get("LOG_TIMEZONE") or "UTC")
    import mail_trace
    mail_trace.install()
    logging.getLogger("mail.log").setLevel(logging.WARNING)
    logging.getLogger("mail.log").addFilter(smtp_rauschen.AbbruchLeiser())

    log.info("EXO SMTP Relay v%s startet (Python %s, %s)", config.VERSION,
             sys.version.split()[0], sys.platform)
    log.info("Datenverzeichnis: %s — PowerShell: %s", config.DATA_DIR, config.PWSH)

    import secure_io
    secure_io.ensure_dir(config.DATA_DIR)
    try:
        secure_io.harden_tree(config.DATA_DIR)
    except Exception as exc:                                  # noqa: BLE001
        log.error("Dateirechte-Härtung fehlgeschlagen: %s", exc)

    import mail_audit
    mail_audit.init_db()
    mail_audit.prune_old_events(int(settings_store.get("LOG_RETENTION_DAYS") or 90))

    import tls_cert
    try:
        st = tls_cert.sicherstellen(settings_store.get("PUBLIC_HOSTNAME") or "")
        log.info("TLS-Zertifikat: %s (gültig bis %s)", ", ".join(st.get("hostnames") or []),
                 st.get("not_after", "?"))
    except Exception as exc:                                  # noqa: BLE001
        log.error("TLS-Zertifikat konnte nicht bereitgestellt werden: %s", exc)

    import relay_hosts
    relay_hosts.aufraeumen()

    threading.Thread(target=_run_webui, name="webui", daemon=True).start()
    log.info("Web-UI auf Port %d gestartet", config.WEBUI_PORT)
    scheduler.start()
    asyncio.run(_run_smtp())


if __name__ == "__main__":
    main()
