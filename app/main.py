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


_ASCII_ERSATZ = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
                               "ß": "ss", "—": "-", "–": "-", "„": '"', "“": '"', "”": '"', "…": "..."})


def smtp_ascii(text: str) -> str:
    """SMTP-Antworttext als ASCII: Umlaute umschreiben, Rest durch „?“."""
    return text.translate(_ASCII_ERSATZ).encode("ascii", "replace").decode("ascii")


class _LenientSMTP(_BaseSMTP):
    """aiosmtpd.SMTP mit zwei Anpassungen aus dem Gateway.

    1. `connection_made`: STARTTLS bleibt angeboten, wird aber für ein
       freigegebenes Relay-Gerät nicht VERLANGT — ein Etikettendrucker von 2011
       kann es nicht, und genau für solche Geräte gibt es diesen Dienst. Seit
       0.2.62 auch nicht für fremde Adressen (sie scheitern bei RCPT mit klarem
       Grund); Pflicht bleibt es nur für die eigenen Adressen des Servers
       (Testmail). Die Entscheidung fällt bei jeder Verbindung neu.
    2. `smtp_MAIL`: unbekannte MAIL-FROM-Parameter werden übergangen statt mit
       555 abgewiesen. Manche Geräte senden `AUTH=<>` oder Ähnliches mit.
    3. `push`: Antwortzeilen nur in ASCII (wie Gateway v1.12.24).
    """

    async def push(self, status):
        """Antwortzeilen nur in ASCII (RFC 5321 §4.2) — Texte im Code dürfen
        Umlaute tragen. aiosmtpd schickt sonst UTF-8 (Outlook zeigte im Gateway
        „Kein Senderecht fÃ¼r …“) oder wirft bei abgeschaltetem SMTPUTF8; dann
        kommt statt der Ablehnung ein generisches „500 Error: (UnicodeEncodeError)“.
        Bytes (mehrzeilige Antworten als Einzelzeilen) laufen unverändert durch."""
        if isinstance(status, str):
            status = smtp_ascii(status)
        await super().push(status)

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
            import testmail
            if smtp_relay.ist_relay_quelle(ip):
                self.require_starttls = False
                log.info("SMTP: %s ist ein freigegebenes Relay-Gerät — "
                         "STARTTLS wird angeboten, aber nicht verlangt", ip)
            elif ip not in testmail.eigene_quellen():
                # Seit 0.2.62: Auch ein NICHT freigegebenes Gerät muss kein STARTTLS — es wird
                # bei RCPT mit klarem Grund abgewiesen (handler.handle_RCPT). Vorher bekam es
                # zuerst „Must issue a STARTTLS command first“ und suchte den Fehler am
                # Zertifikat. Vor DATA wird nichts angenommen; Anmeldedaten gibt es keine.
                self.require_starttls = False
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


def _tls_stempel() -> tuple | None:
    """Kennung des Zertifikatspaares auf der Platte (Änderungszeit, Größe) —
    None, solange eines der beiden fehlt."""
    try:
        return tuple((s.st_mtime_ns, s.st_size) for s in
                     (Path(config.SMTP_TLS_CERT).stat(), Path(config.SMTP_TLS_KEY).stat()))
    except OSError:
        return None


class _LenientController(Controller):
    """Controller, der ein geändertes TLS-Zertifikat ohne Neustart übernimmt.

    ⚠️ Bis v0.2.36 baute `_run_smtp` den TLS-Kontext EINMAL beim Start. Ein neues
    Zertifikat (PFX-Import, selbstsigniert neu, Let's Encrypt samt automatischer
    Erneuerung, Wiederherstellung aus dem Backup) lag danach auf der Platte, der
    Listener bot aber bis zum nächsten Neustart das alte an — bei einer
    Erneuerung nachts im Zeitplaner also das ablaufende.

    Jetzt vergleicht `factory()` vor jeder neuen Verbindung die Kennung der
    beiden Dateien (`_tls_stempel`) und baut bei einer Änderung den Kontext neu.
    Das deckt JEDEN Schreibweg ab, auch künftige, ohne dass einer daran denken
    muss. Passen Zertifikat und Schlüssel (noch) nicht zusammen — zwischen den
    beiden atomaren Schreibvorgängen —, bleibt der alte Kontext und die nächste
    Verbindung versucht es erneut. Bestehende Verbindungen behalten ihren.
    Der Rückweg zu Exchange (`smarthost._client_tls_context`) liest das
    Zertifikat ohnehin bei jedem Versand."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Aufrufer übergeben den Kontext aus den aktuellen Dateien.
        self._tls_stempel = _tls_stempel()

    def tls_auffrischen(self) -> bool:
        """Kontext neu bauen, falls sich das Zertifikat geändert hat. True = neu."""
        stempel = _tls_stempel()
        if stempel == self._tls_stempel:
            return False
        try:
            ctx = _build_tls_context()
        except Exception as exc:                              # noqa: BLE001
            log.warning("TLS-Zertifikat geändert, aber nicht ladbar (%s) — "
                        "bisheriges bleibt", exc)
            return False
        self.SMTP_kwargs["tls_context"] = ctx
        self.SMTP_kwargs["require_starttls"] = ctx is not None
        self._tls_stempel = stempel
        log.info("TLS-Zertifikat neu geladen — neue Verbindungen nutzen es (STARTTLS: %s)",
                 "ja" if ctx else "nein")
        return True

    def factory(self):
        self.tls_auffrischen()
        return _LenientSMTP(self.handler, **self.SMTP_kwargs)

    def _trigger_server(self):
        # aiosmtpd verbindet sich zur Bereitschaftspruefung mit dem Bind-Namen. Unter
        # Windows ist 0.0.0.0 als Verbindungsziel ungueltig (WinError 10049) -
        # dann stuerzt der Start ab. Fuer die Pruefung genuegt Loopback.
        gebunden = self.hostname
        if gebunden in ("0.0.0.0", "::"):
            self.hostname = "127.0.0.1" if gebunden == "0.0.0.0" else "::1"
        try:
            super()._trigger_server()
        finally:
            self.hostname = gebunden


# Cipherliste der Geräteseite — Begründung in _build_tls_context().
GERAETE_CIPHERS = "DEFAULT:@SECLEVEL=0"


def _build_tls_context() -> ssl.SSLContext | None:
    cert, key = Path(config.SMTP_TLS_CERT), Path(config.SMTP_TLS_KEY)
    if not cert.exists() or not key.exists():
        log.warning("TLS-Zertifikat fehlt (%s / %s) — SMTP läuft OHNE STARTTLS", cert, key)
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    # Alte Geräte: TLS 1.0/1.1 und statischen RSA-Schlüsseltausch zulassen. Ein
    # Scanner von 2012 kann nichts Besseres, und Klartext wäre die Alternative —
    # nicht ein neueres TLS.
    #
    # Die Mindestversion allein genügt unter OpenSSL 3 NICHT (gemessen
    # 2026-10-11): Sicherheitsstufe 2 verbietet TLS 1.0/1.1 und deren
    # SHA-1-Signaturen, und mit Pythons eigener Cipherliste blieb unter
    # OpenSSL 3.5 (im Abbild) keine kRSA-Suite übrig, unter 3.0 noch sechs — ein
    # HP-Gerät, das nur TLS_RSA_WITH_AES_128_CBC_SHA256 kann, scheiterte im
    # Abbild am Handshake. Deshalb
    # OpenSSLs DEFAULT-Liste auf Stufe 0. Sie ordnet ECDHE/AES-GCM vorn ein, und
    # create_default_context setzt OP_CIPHER_SERVER_PREFERENCE: Moderne Clients
    # bekommen weiter TLS 1.3 bzw. Forward Secrecy, die alten Suites sind nur
    # Rückfallebene. NULL-/anonyme Suites enthält DEFAULT nicht.
    #
    # Nur HIER, auf der Geräteseite. Der Weg zu Exchange (smarthost.py) und die
    # Weboberfläche bleiben bei den strengen Vorgaben.
    # kRSA setzt ein RSA-Zertifikat voraus (selbstsigniert: immer RSA; bei einem
    # importierten EC-Zertifikat gehen nur die ECDHE-Suites).
    ctx.set_ciphers(GERAETE_CIPHERS)
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
    # Lebenszeichen für den Tauscher (update_tausch.gesund_abwarten): Erst
    # jetzt bedient der Listener — eine Fassung, die vorher abstürzt, wird
    # zurückgerollt.
    import update_tausch
    update_tausch.lebenszeichen_schreiben(Path(config.DATA_DIR), config.VERSION)
    # Nachbereitung anfordern, falls ein älterer Updater diese Fassung eingespielt hat (Units,
    # data\\-Rechte, Dienst-Wrapper) — verzögert, nie den Start blockierend.
    def _nachbereitung():
        try:
            import aktualisierung
            aktualisierung.nachbereitung_anfordern()
        except Exception as exc:                              # noqa: BLE001
            log.warning("Nachbereitung nicht angefordert: %s", exc)
    import aktualisierung as _akt
    _t = threading.Timer(_akt.NACHBEREITUNG_VERZOEGERUNG_S, _nachbereitung)
    _t.daemon = True
    _t.start()

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


class _ProactorRauschen(logging.Filter):
    """Windows (Proactor): ein Client, der die Verbindung hart schliesst (Browser, Scanner,
    Portscan), erzeugt beim Aufraeumen einen ERROR mit Traceback - ohne Folgen fuer den Dienst."""

    def filter(self, satz: logging.LogRecord) -> bool:
        ausnahme = satz.exc_info[1] if satz.exc_info else None
        return not (isinstance(ausnahme, ConnectionResetError)
                    and "_call_connection_lost" in satz.getMessage())


def main() -> None:
    logging.getLogger("asyncio").addFilter(_ProactorRauschen())
    settings_store.init(config._ENV_SEEDS)
    log_manager.setup(retention_days=int(settings_store.get("LOG_RETENTION_DAYS") or 30),
                      tz_name=settings_store.get("LOG_TIMEZONE") or "UTC")
    log_manager.stufe_anwenden()
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

    import startmenue                                          # nur Windows; fuehrt den Link nach (z. B. nach einer Neuinstallation)
    startmenue.aktualisieren(settings_store.get("PUBLIC_HOSTNAME") or "")

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
