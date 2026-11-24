"""TLS-Zertifikat des Listeners — selbstsigniert erzeugen oder aus PFX übernehmen.

Ohne Zertifikat gäbe es kein STARTTLS, und dann liefe JEDE Einlieferung im
Klartext. Deshalb erzeugt `sicherstellen()` beim ersten Start ein
selbstsigniertes Zertifikat auf den konfigurierten Hostnamen. Für Geräte im
eigenen Netz genügt das: Ein Drucker prüft den Aussteller nicht.

Für den Rückweg zu Exchange reicht ein selbstsigniertes Zertifikat NICHT, wenn
der Inbound-Connector den Dienst am Zertifikat erkennt (`TlsSenderCertificateName`):
Microsoft verlangt dafür „a valid certification authority-signed (CA-signed)
certificate“, dessen Subject oder SAN eine akzeptierte Domäne des Tenants
enthält (MS Learn: „Set up connectors to route mail …“, Prerequisites, Punkt 2;
„How to set up a multifunction device or application …“, TLS certificate-based
connector). Mit selbstsigniertem Zertifikat bleibt die Adressvariante
(`SenderIPAddresses`, feste öffentliche IP); `exo_setup.connector_einrichten`
lässt die Zertifikatsvariante dann nicht zu.
Wer ein Zertifikat einer CA hat, importiert es als PFX — oder holt eines von
Let's Encrypt (`le_certbot.py` HTTP-01, `tls_acme_dns.py` DNS-01). Ein
Zertifikat aus der TESTUMGEBUNG von Let's Encrypt („(STAGING)“ im Aussteller)
stammt von einer nicht vertrauenswürdigen CA und zählt hier NICHT als
CA-signiert (`info()["ca_signiert"]`).

EIN SCHREIBWEG: Selbstsigniert, PFX, Let's Encrypt — alle schreiben über
`_schreiben`. Der Listener lädt ein geändertes Paar bei der nächsten
Verbindung von selbst (`main._LenientController.tls_auffrischen`).

Alles mit `cryptography`, ohne `openssl`-Binary — das gibt es unter Windows
nicht verlässlich.
"""
from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path

import config
import secure_io

log = logging.getLogger(__name__)


def cert_hostnames(cert) -> list[str]:
    from cryptography import x509
    from cryptography.x509.oid import ExtensionOID, NameOID
    namen: list[str] = []
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        namen += san.value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        pass
    try:
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if cn:
            namen.append(cn[0].value)
    except Exception:                                         # noqa: BLE001
        pass
    out: list[str] = []
    for n in namen:
        n = (n or "").strip().lower()
        if n and n not in out:
            out.append(n)
    return out


def host_matches(host: str, namen: list[str]) -> bool:
    """RFC 6125: ein Wildcard deckt genau EINE Ebene ab."""
    host = (host or "").strip().lower()
    if not host:
        return False
    for n in namen:
        if n == host:
            return True
        if n.startswith("*.") and "." in host and host.split(".", 1)[1] == n[2:]:
            return True
    return False


def _schreiben(cert_pem: bytes, key_pem: bytes) -> None:
    cert_pfad, key_pfad = Path(config.SMTP_TLS_CERT), Path(config.SMTP_TLS_KEY)
    secure_io.ensure_dir(cert_pfad.parent)
    # Das Zertifikat ist öffentlich, der Schlüssel nicht — beide atomar.
    tmp = cert_pfad.with_name(cert_pfad.name + ".tmp")
    tmp.write_bytes(cert_pem)
    tmp.replace(cert_pfad)
    secure_io.write_secret_bytes(key_pfad, key_pem)


def selbstsigniert_erzeugen(hostname: str, tage: int = 3650) -> dict:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    hostname = (hostname or "").strip().lower() or "exo-smtp-relay.local"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    jetzt = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(jetzt - _dt.timedelta(minutes=5))
            .not_valid_after(jetzt + _dt.timedelta(days=tage))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    _schreiben(cert.public_bytes(serialization.Encoding.PEM),
               key.private_bytes(serialization.Encoding.PEM,
                                 serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()))
    log.info("Selbstsigniertes TLS-Zertifikat für %s erzeugt (%d Tage)", hostname, tage)
    return info()


def install_pfx(pfx_bytes: bytes, password: str, expected_host: str = "",
                allow_mismatch: bool = False) -> dict:
    """PFX entpacken, gegen den Hostnamen prüfen, als cert.pem/key.pem ablegen."""
    from cryptography.hazmat.primitives.serialization import (
        pkcs12, Encoding, PrivateFormat, NoEncryption)
    pw = password.encode() if password else None
    try:
        key, cert, chain = pkcs12.load_key_and_certificates(pfx_bytes, pw)
    except Exception as exc:                                  # noqa: BLE001
        raise ValueError(f"PFX nicht lesbar (falsches Passwort?): {exc}") from exc
    if key is None or cert is None:
        raise ValueError("PFX enthält kein Zertifikat mit privatem Schlüssel.")
    namen = cert_hostnames(cert)
    warnung = ""
    if expected_host and not host_matches(expected_host, namen):
        meldung = (f"Zertifikat passt nicht zum Hostnamen {expected_host!r} "
                   f"(enthält: {', '.join(namen) or 'keine DNS-Namen'}).")
        if not allow_mismatch:
            raise ValueError(meldung + " Ist der Import trotzdem gewollt, "
                             "die Option 'Prüfung übergehen' aktivieren.")
        warnung = meldung
    cert_pem = cert.public_bytes(Encoding.PEM) + b"".join(
        c.public_bytes(Encoding.PEM) for c in (chain or []))
    key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    _schreiben(cert_pem, key_pem)
    aus = info()
    aus["warnung"] = warnung
    return aus


def info() -> dict:
    """Was liegt gerade da? Für die Oberfläche."""
    cert_pfad, key_pfad = Path(config.SMTP_TLS_CERT), Path(config.SMTP_TLS_KEY)
    if not (cert_pfad.exists() and key_pfad.exists()):
        return {"vorhanden": False}
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(cert_pfad.read_bytes())
        selbst = cert.issuer == cert.subject
        aussteller = cert.issuer.rfc4514_string()
        staging = "(STAGING)" in aussteller.upper()
        rest = cert.not_valid_after_utc - _dt.datetime.now(_dt.timezone.utc)
        return {"vorhanden": True, "hostnames": cert_hostnames(cert),
                "not_after": cert.not_valid_after_utc.isoformat(),
                "tage_rest": rest.days,
                "selbstsigniert": selbst,
                "staging": staging,
                "ca_signiert": not selbst and not staging,
                "lets_encrypt": "let's encrypt" in aussteller.lower(),
                "aussteller": aussteller}
    except Exception as exc:                                  # noqa: BLE001
        return {"vorhanden": True, "fehler": str(exc)}


def sicherstellen(hostname: str) -> dict:
    """Beim Start: ohne Zertifikat ein selbstsigniertes anlegen."""
    st = info()
    if st.get("vorhanden") and not st.get("fehler"):
        return st
    return selbstsigniert_erzeugen(hostname)
