"""Der Listener übernimmt ein neues TLS-Zertifikat ohne Neustart (v0.2.37).

Bis v0.2.36 baute `_run_smtp` den Kontext einmal beim Start. Ein neues
Zertifikat — ob von Hand, aus dem Backup oder durch die automatische
Erneuerung von Let's Encrypt — lag dann auf der Platte, der Listener bot aber
das alte an. Geprüft wird mit einem ECHTEN STARTTLS-Handshake, welches
Zertifikat der Listener ausliefert.
"""
import smtplib
import socket
import ssl

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID


def _freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Still:
    async def handle_DATA(self, server, session, envelope):
        return "250 OK"


@pytest.fixture
def listener(monkeypatch, tmp_path):
    import config
    import main
    import tls_cert

    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    tls_cert.selbstsigniert_erzeugen("alt.firma.de")
    port = _freier_port()
    ctl = main._LenientController(_Still(), hostname="127.0.0.1", port=port,
                                  tls_context=main._build_tls_context(), require_starttls=True)
    ctl.start()
    try:
        yield port
    finally:
        ctl.stop()


def _angebotener_name(port: int) -> str:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with smtplib.SMTP("127.0.0.1", port, timeout=10) as s:
        s.ehlo()
        s.starttls(context=ctx)
        der = s.sock.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der)
    return cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value


def test_neues_zertifikat_wirkt_ohne_neustart(listener):
    import tls_cert
    assert _angebotener_name(listener) == "alt.firma.de"
    tls_cert.selbstsigniert_erzeugen("neu.firma.de")
    assert _angebotener_name(listener) == "neu.firma.de"


def test_halb_geschriebenes_paar_behaelt_das_alte(listener, tmp_path):
    """Zertifikat schon neu, Schlüssel noch alt (zwischen zwei atomaren
    Schreibvorgängen): Der Listener bleibt beim alten Kontext statt
    STARTTLS zu verlieren — und übernimmt, sobald das Paar passt."""
    import config
    import tls_cert
    alt_key = (tmp_path / "key.pem").read_bytes()
    tls_cert.selbstsigniert_erzeugen("neu.firma.de")
    neu_key = (tmp_path / "key.pem").read_bytes()
    (tmp_path / "key.pem").write_bytes(alt_key)
    assert _angebotener_name(listener) == "alt.firma.de"
    with open(config.SMTP_TLS_KEY, "wb") as f:
        f.write(neu_key)
    assert _angebotener_name(listener) == "neu.firma.de"
