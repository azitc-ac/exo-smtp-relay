"""Selbstsigniertes Zertifikat passt zum Startmenü-Link (0.2.43).

Labtest 0.2.42 (Windows): Der Link aus install.ps1 zeigte auf
https://<FQDN>:8443, das beim ersten Start erzeugte Zertifikat lautete auf
exo-smtp-relay.local. Jetzt trägt es FQDN und kurzen Rechnernamen als SAN.
"""
from cryptography import x509

import tls_cert
from conftest import APP  # noqa: F401  (sys.path)


def _san() -> list[str]:
    from pathlib import Path
    import config
    cert = x509.load_pem_x509_certificate(Path(config.SMTP_TLS_CERT).read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return san.get_values_for_type(x509.DNSName)


def _rechner(monkeypatch, fqdn, kurz, art="windows"):
    monkeypatch.setenv("RELAY_BETRIEBSART", art)
    monkeypatch.setattr(tls_cert.socket, "getfqdn", lambda *a: fqdn)
    monkeypatch.setattr(tls_cert.socket, "gethostname", lambda: kurz)


def test_ohne_hostname_fqdn_und_kurzname(monkeypatch):
    _rechner(monkeypatch, "SRV2.ad.firma.de", "SRV2")
    info = tls_cert.selbstsigniert_erzeugen("")
    assert _san() == ["srv2.ad.firma.de", "srv2"]
    assert info["hostnames"][0] == "srv2.ad.firma.de"


def test_eingetragener_hostname_vorn_rechnernamen_dazu(monkeypatch):
    _rechner(monkeypatch, "srv2.ad.firma.de", "srv2", art="systemd")
    tls_cert.selbstsigniert_erzeugen("relay.firma.de")
    assert _san() == ["relay.firma.de", "srv2.ad.firma.de", "srv2"]


def test_im_container_keine_container_id(monkeypatch):
    _rechner(monkeypatch, "3f2a9c1b7d4e", "3f2a9c1b7d4e", art="docker")
    tls_cert.selbstsigniert_erzeugen("")
    assert _san() == [tls_cert.VORGABE_NAME]


def test_localhost_wird_nie_eingetragen(monkeypatch):
    _rechner(monkeypatch, "localhost.localdomain", "localhost")
    tls_cert.selbstsigniert_erzeugen("")
    assert _san() == [tls_cert.VORGABE_NAME]
