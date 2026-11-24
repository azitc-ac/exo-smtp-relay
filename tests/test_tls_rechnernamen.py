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


def test_langer_fqdn_scheitert_nicht(monkeypatch):
    """CI 0.2.46: FQDN des Windows-Runners 65 Zeichen → „Attribute's length must be <= 64“,
    kein Zertifikat, Oberfläche ohne TLS. Jetzt: CN ist der erste Name ≤ 64 Zeichen, der
    lange FQDN steht trotzdem im SAN."""
    import tls_cert
    from cryptography import x509
    lang = "runnervm0abcdefghijk." + "x" * 30 + ".internal.cloudapp.net"
    assert len(lang) > 64
    from pathlib import Path
    import config
    _rechner(monkeypatch, lang, "runnervm0abcdefghijk")
    tls_cert.selbstsigniert_erzeugen("")
    cert = x509.load_pem_x509_certificate(Path(config.SMTP_TLS_CERT).read_bytes())
    cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
    assert cn == "runnervm0abcdefghijk"
    assert _san() == [lang, "runnervm0abcdefghijk"]
    assert tls_cert.cn_waehlen(["a" * 70]) == tls_cert.VORGABE_NAME


def test_acme_csr_mit_langem_namen():
    import tls_acme_dns
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ec
    lang = "a" * 40 + "." + "b" * 30 + ".example"
    csr = x509.load_der_x509_csr(tls_acme_dns._build_csr(ec.generate_private_key(ec.SECP256R1()), lang))
    assert csr.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME) == []
    assert csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == [lang]
