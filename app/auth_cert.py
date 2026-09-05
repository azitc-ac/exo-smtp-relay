"""Auth-Zertifikat für die App-Registrierung — Anmeldung an Exchange Online.

Das ExchangeOnlineManagement-Modul meldet sich als Anwendung mit einem
Zertifikat an (`Connect-ExchangeOnline -Certificate`). Das Zertifikat wird HIER
erzeugt; der öffentliche Teil (`.cer`) wird in Entra bei der App-Registrierung
hochgeladen, der private bleibt als `auth.pfx` (ohne Passwort, Rechte 600) im
Datenverzeichnis. Genau wie im Gateway — nur ohne `openssl`-Binary, das es
unter Windows nicht gibt: `cryptography` kann PKCS#12 selbst.

⚠️ Kein Passwort auf der PFX, mit Absicht: Das Passwort müsste daneben liegen,
im Klartext, in derselben Datei mit denselben Rechten. Es schützte dann nichts
und wäre nur ein zweiter Ort, an dem etwas verloren gehen kann.
"""
from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path

import config
import secure_io

log = logging.getLogger(__name__)

PFX_PATH = Path(config.DATA_DIR) / "auth.pfx"
CN = "EXO-SMTP-Relay"


def erzeugen(tage: int = 3650) -> dict:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CN)])
    jetzt = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(jetzt - _dt.timedelta(minutes=5))
            .not_valid_after(jetzt + _dt.timedelta(days=tage))
            .sign(key, hashes.SHA256()))
    pfx = pkcs12.serialize_key_and_certificates(
        name=CN.encode(), key=key, cert=cert, cas=None,
        encryption_algorithm=serialization.NoEncryption())
    secure_io.write_secret_bytes(PFX_PATH, pfx)
    log.info("Auth-Zertifikat erzeugt: %s (gültig %d Tage)", PFX_PATH, tage)
    return info()


def _laden():
    from cryptography.hazmat.primitives.serialization import pkcs12
    key, cert, _ = pkcs12.load_key_and_certificates(PFX_PATH.read_bytes(), None)
    return key, cert


def public_cer() -> bytes | None:
    """Der öffentliche Teil als DER — zum Hochladen in Entra."""
    if not PFX_PATH.exists():
        return None
    from cryptography.hazmat.primitives import serialization
    _, cert = _laden()
    return cert.public_bytes(serialization.Encoding.DER)


def info() -> dict:
    if not PFX_PATH.exists():
        return {"vorhanden": False}
    try:
        from cryptography.hazmat.primitives import hashes
        _, cert = _laden()
        return {"vorhanden": True,
                "thumbprint": cert.fingerprint(hashes.SHA1()).hex().upper(),
                "not_after": cert.not_valid_after_utc.isoformat(),
                "subject": cert.subject.rfc4514_string()}
    except Exception as exc:                                  # noqa: BLE001
        return {"vorhanden": True, "fehler": str(exc)}


def importieren(pfx_bytes: bytes, password: str) -> dict:
    """Eine vorhandene PFX übernehmen — z. B. die des grossen Gateways, wenn
    dieselbe App-Registrierung genutzt werden soll. Wird ohne Passwort
    neu geschrieben (siehe Modulkopf)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    try:
        key, cert, cas = pkcs12.load_key_and_certificates(
            pfx_bytes, password.encode() if password else None)
    except Exception as exc:                                  # noqa: BLE001
        raise ValueError(f"PFX nicht lesbar (falsches Passwort?): {exc}") from exc
    if key is None or cert is None:
        raise ValueError("PFX enthält kein Zertifikat mit privatem Schlüssel.")
    neu = pkcs12.serialize_key_and_certificates(
        name=CN.encode(), key=key, cert=cert, cas=cas or None,
        encryption_algorithm=serialization.NoEncryption())
    secure_io.write_secret_bytes(PFX_PATH, neu)
    log.info("Auth-Zertifikat importiert: %s", PFX_PATH)
    return info()
