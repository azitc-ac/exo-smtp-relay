"""Altgeräte bekommen auf der Geräteseite wirklich TLS 1.0/1.1 und statisches RSA.

ANLASS (2026-10-11)
-------------------
`_build_tls_context()` setzte nur `minimum_version = TLSv1`. Unter OpenSSL 3
genügt das nicht: Sicherheitsstufe 2 verbietet TLS 1.0/1.1, und mit Pythons
Cipherliste blieb unter OpenSSL 3.5 (im ausgelieferten Abbild) keine
kRSA-Suite übrig. Ein HP-Gerät, das nur TLS_RSA_WITH_AES_128_CBC_SHA256
spricht, scheiterte am STARTTLS-Handshake. Der Kommentar versprach das
Gegenteil, und `test_relay_tls.py` prüft nur die STARTTLS-Pflicht, keinen
Handshake — deshalb fiel es nicht auf.

Diese Tests machen einen ECHTEN Handshake gegen den Kontext des Dienstes. Die
Gegenrichtung gehört dazu: Ein moderner Client muss weiter Forward Secrecy
aushandeln, die alten Suites sind nur Rückfallebene.
"""
import socket
import ssl
import sys
import threading
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


@pytest.fixture
def server_ctx(monkeypatch, tmp_path):
    import config
    import main
    import tls_cert

    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    tls_cert.selbstsigniert_erzeugen("relay.firma.de")
    ctx = main._build_tls_context()
    assert ctx is not None
    return ctx


def _handshake(server_ctx: ssl.SSLContext, client_ctx: ssl.SSLContext) -> tuple[str, str]:
    """Liefert (Protokoll, Suite) — oder wirft den Fehler des Clients."""
    lauscher = socket.socket()
    lauscher.bind(("127.0.0.1", 0))
    lauscher.listen(1)
    lauscher.settimeout(10)
    port = lauscher.getsockname()[1]

    def _server():
        try:
            verbindung, _ = lauscher.accept()
        except OSError:
            return
        try:
            with server_ctx.wrap_socket(verbindung, server_side=True) as t:
                t.recv(1)
        except (ssl.SSLError, OSError):
            pass      # der Client meldet den Fehler, der zählt
        finally:
            verbindung.close()

    faden = threading.Thread(target=_server, daemon=True)
    faden.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as roh:
            with client_ctx.wrap_socket(roh, server_hostname="relay.firma.de") as t:
                ergebnis = (t.version(), t.cipher()[0])
                t.send(b"x")
        return ergebnis
    finally:
        faden.join(10)
        lauscher.close()


def _client(ciphers: str | None = None, hoechstens=None) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if ciphers:
        ctx.set_ciphers(ciphers)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)   # TLSv1 ist absichtlich
        if hoechstens is not None:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
            ctx.maximum_version = hoechstens
    return ctx


def _client_kann(ciphers: str, hoechstens) -> ssl.SSLContext:
    """Client-Kontext für ein Altgerät — oder skip, wenn das lokale OpenSSL
    diesen Client grundsätzlich nicht bauen kann (dann ist der Test nicht
    aussagekräftig, aber auch nicht still grün)."""
    try:
        ctx = _client(ciphers, hoechstens)
    except (ssl.SSLError, ValueError) as e:
        pytest.skip(f"lokales OpenSSL kann diesen Client nicht bauen: {e}")
    if not ctx.get_ciphers():
        pytest.skip("lokales OpenSSL bietet für diesen Client keine Suite an")
    return ctx


def test_hp_geraet_nur_tls_rsa_with_aes_128_cbc_sha256(server_ctx):
    """Die Suite alter HP-MFPs: statischer RSA-Schlüsseltausch, TLS 1.2."""
    client = _client_kann("AES128-SHA256:@SECLEVEL=0", ssl.TLSVersion.TLSv1_2)
    assert _handshake(server_ctx, client) == ("TLSv1.2", "AES128-SHA256")


@pytest.mark.parametrize("version, name", [
    (ssl.TLSVersion.TLSv1, "TLSv1"),
    (ssl.TLSVersion.TLSv1_1, "TLSv1.1"),
])
def test_tls_1_0_und_1_1(server_ctx, version, name):
    client = _client_kann("DEFAULT:@SECLEVEL=0", version)
    assert _handshake(server_ctx, client)[0] == name


def test_tls_1_0_nur_statisches_rsa(server_ctx):
    """Das älteste Gerät: TLS 1.0 und nichts als AES128-SHA (kRSA)."""
    client = _client_kann("AES128-SHA:@SECLEVEL=0", ssl.TLSVersion.TLSv1)
    assert _handshake(server_ctx, client) == ("TLSv1", "AES128-SHA")


def test_moderner_client_bekommt_tls_1_3(server_ctx):
    version, _ = _handshake(server_ctx, _client())
    assert version == "TLSv1.3"


def test_client_der_alles_kann_bekommt_forward_secrecy(server_ctx):
    """Bietet ein Client bis TLS 1.2 ALLES an, auch die alten Suites, entscheidet
    die Serverpräferenz — und die muss ECDHE mit AEAD wählen, nicht kRSA/CBC."""
    client = _client_kann("ALL:@SECLEVEL=0", ssl.TLSVersion.TLSv1_2)
    version, suite = _handshake(server_ctx, client)
    assert version == "TLSv1.2"
    assert suite.startswith("ECDHE-") and ("GCM" in suite or "CHACHA20" in suite), suite


def test_keine_null_oder_anonymen_suites(server_ctx):
    """Stufe 0 lockert die Mindeststärke — DEFAULT darf trotzdem weder
    unverschlüsselte noch unauthentifizierte Suites enthalten."""
    namen = [c["name"] for c in server_ctx.get_ciphers()]
    assert namen
    assert not [n for n in namen if "NULL" in n or n.startswith(("ADH-", "AECDH-"))
                or "RC4" in n or "DES" in n], namen
    assert server_ctx.options & ssl.OP_CIPHER_SERVER_PREFERENCE


def test_exchange_seite_bleibt_streng():
    """Die Lockerung gehört nur auf die Geräteseite. smarthost.py baut seine
    Kontexte mit den Vorgaben — kein SECLEVEL, keine eigene Cipherliste."""
    quelle = (Path(__file__).resolve().parent.parent / "app" / "smarthost.py").read_text(encoding="utf-8")
    assert "SECLEVEL" not in quelle and "set_ciphers" not in quelle
    assert "minimum_version" not in quelle
