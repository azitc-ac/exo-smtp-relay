"""Ein Port-Scanner darf die Fehlerstufe nicht unbrauchbar machen.

ANLASS (24.08.2026)
-------------------
Im Protokoll der Produktions-VM (24.07.–22.08.) waren **143 von 144 ERROR-Zeilen**
abgebrochene Fremdverbindungen, jede mit vollständigem Traceback, jede beim
TLS-Handshake. Wer nach einem Betriebsfehler suchte, fand 143 Scanner und einen
Befund — die Stufe ERROR war als Suchmerkmal wertlos.

Gefährlich war das nie: Die Quell-IP-Prüfung sitzt in `handler.handle_DATA`,
und diese Verbindungen brechen lange davor ab. Ein reines Diagnoseproblem — aber
eines, das jede Fehlersuche im Protokoll behindert.

Die Prüfung hier deckt beide Richtungen ab: Das Rauschen wird leiser, ein echter
Fehler bleibt ein Fehler. Der zweite Teil ist der wichtigere — ein Filter, der zu
viel schluckt, wäre schlimmer als das Rauschen.
"""
import asyncio
import logging
import ssl
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import smtp_rauschen  # noqa: E402


def _satz(fehler: BaseException | None,
          meldung: str = "%r SMTP session exception") -> logging.LogRecord:
    """Ein Datensatz, wie aiosmtpd ihn erzeugt (smtp.py: log.exception(...))."""
    r = logging.LogRecord("mail.log", logging.ERROR, "smtp.py", 608,
                          meldung, (("203.0.113.9", 61000),),
                          (type(fehler), fehler, None) if fehler else None)
    return r


@pytest.fixture
def leiser():
    return smtp_rauschen.AbbruchLeiser()


@pytest.mark.parametrize("fehler", [
    ssl.SSLError("handshake failure"),
    ConnectionResetError("peer went away"),
    BrokenPipeError(),
    TimeoutError(),
    asyncio.IncompleteReadError(b"", 4),
])
def test_abbruch_wird_leiser(leiser, fehler):
    r = _satz(fehler)
    assert leiser.filter(r) is True, "die Zeile bleibt — sie wird nur leiser"
    assert r.levelno == logging.INFO
    assert r.levelname == "INFO"
    assert r.exc_info is None, "kein Traceback für einen abgebrochenen Handshake"
    assert r.exc_text is None, (
        "exc_text muss mit weg — sonst hängt ein bereits formatierter Traceback "
        "trotzdem an der Ausgabe.")


def test_gegenstelle_bleibt_erhalten(leiser):
    """Ohne die Adresse liesse sich ein Angriff nicht von Rauschen trennen —
    genau deshalb ein Filter statt des Handler-Hooks, der sie nicht kennt."""
    r = _satz(ssl.SSLError("x"))
    leiser.filter(r)
    assert "203.0.113.9" in (r.msg % r.args)


def test_echter_fehler_bleibt_fehler(leiser):
    """Der wichtigere Teil: Ein Filter, der zu viel schluckt, ist schlimmer als
    das Rauschen, das er beseitigen soll."""
    r = _satz(ValueError("kaputte Kopfzeile"))
    assert leiser.filter(r) is True
    assert r.levelno == logging.ERROR, "unbeteiligte Ausnahmen bleiben ERROR"
    assert r.exc_info is not None, "und behalten ihren Traceback"


def test_fremde_meldung_bleibt_unangetastet(leiser):
    """Der Filter hängt am Wortlaut von aiosmtpd. Trifft er nicht, darf er auch
    nichts anfassen — dann ist es wieder laut, aber nichts geht verloren."""
    r = _satz(ssl.SSLError("x"), meldung="%r irgendetwas anderes")
    assert leiser.filter(r) is True
    assert r.levelno == logging.ERROR


def test_ohne_ausnahme_unveraendert(leiser):
    """`log.error(...)` ohne exc_info ist kein Verbindungsabbruch."""
    r = _satz(None)
    assert leiser.filter(r) is True
    assert r.levelno == logging.ERROR


def test_filter_haengt_am_logger():
    """Die Verdrahtung selbst — sonst ist der Filter fertig und wirkungslos.

    Gesucht wird die Zeile in `main.py`; ein Test, der nur die Klasse prüft,
    hätte den vergessenen `addFilter()` nicht bemerkt.
    """
    quelle = (WURZEL / "app" / "main.py").read_text("utf-8")
    assert "smtp_rauschen.AbbruchLeiser()" in quelle
    assert 'getLogger("mail.log").addFilter' in quelle


# ── Die Hülle, die aiosmtpd wirklich wirft ───────────────────────────────────
# Bis 06.10.2026 griff der Filter im Betrieb NIE: aiosmtpd meldet nicht
# `SSLError`, sondern `TLSSetupException() from SSLError`. Die Tests oben bauen
# ihre Ausnahme selbst und konnten das nicht sehen.

def _gehuellt(innen: BaseException) -> BaseException:
    from aiosmtpd.smtp import TLSSetupException
    try:
        try:
            raise innen
        except BaseException as e:
            raise TLSSetupException() from e
    except TLSSetupException as h:
        return h


def test_tls_huelle_um_harmlosen_abbruch_wird_leiser(leiser):
    r = _satz(_gehuellt(ssl.SSLError("no shared cipher")))
    leiser.filter(r)
    assert r.levelno == logging.INFO and r.exc_info is None
    assert "SSLError" in r.getMessage()


def test_tls_huelle_um_echten_fehler_bleibt_fehler(leiser):
    r = _satz(_gehuellt(ValueError("eigener Fehler im Handshake-Code")))
    leiser.filter(r)
    assert r.levelno == logging.ERROR and r.exc_info is not None


def test_echter_starttls_abbruch_mit_aiosmtpd(tmp_path):
    """Echter Verkehr: aiosmtpd-Server mit TLS, ein Gegenüber bricht nach
    STARTTLS ab (wie die Scanner im Betrieb). Was im Protokoll landet, muss
    INFO ohne Traceback sein."""
    import datetime
    import socket
    import time
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from aiosmtpd.controller import Controller

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    jetzt = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(jetzt).not_valid_after(jetzt + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")

    gesehen: list[logging.LogRecord] = []

    class Sammler(logging.Handler):
        def emit(self, record):
            gesehen.append(record)

    lg = logging.getLogger("mail.log")
    filt, sammler = smtp_rauschen.AbbruchLeiser(), Sammler()
    lg.addFilter(filt); lg.addHandler(sammler)
    class Leer:
        async def handle_DATA(self, server, session, envelope):
            return "250 OK"
    with socket.socket() as frei:
        frei.bind(("127.0.0.1", 0))
        port = frei.getsockname()[1]
    ctl = Controller(Leer(), hostname="127.0.0.1", port=port, tls_context=ctx)
    try:
        ctl.start()
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            f = s.makefile("rb")
            f.readline()
            s.sendall(b"EHLO scanner\r\n")
            while not f.readline().startswith(b"250 "):
                pass
            s.sendall(b"STARTTLS\r\n")
            assert f.readline().startswith(b"220")
            s.sendall(b"\x16\x03\x01\x00\x05kaputt-kein-tls")
        ende = time.time() + 5
        while time.time() < ende and not any(smtp_rauschen.MELDUNG in str(r.msg) or
                                              "ohne Datenübergabe" in str(r.msg) for r in gesehen):
            time.sleep(0.05)
    finally:
        ctl.stop()
        lg.removeFilter(filt); lg.removeHandler(sammler)

    treffer = [r for r in gesehen if "ohne Datenübergabe" in str(r.msg)
               or smtp_rauschen.MELDUNG in str(r.msg)]
    assert treffer, f"kein Abbruch protokolliert: {[r.getMessage() for r in gesehen]}"
    for r in treffer:
        assert r.levelno == logging.INFO, f"{r.levelname}: {r.getMessage()}"
        assert r.exc_info is None
