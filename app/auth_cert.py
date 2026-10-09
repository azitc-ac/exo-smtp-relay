"""Auth-Zertifikat für die App-Registrierung — Anmeldung an Exchange Online.

Das ExchangeOnlineManagement-Modul meldet sich als Anwendung mit einem
Zertifikat an (`Connect-ExchangeOnline -Certificate`). Das Zertifikat wird HIER
erzeugt; der öffentliche Teil (`.cer`) wird in Entra bei der App-Registrierung
hochgeladen, der private bleibt als `auth.pfx` (ohne Passwort, Rechte 600) im
Datenverzeichnis. Genau wie im Gateway — nur ohne `openssl`-Binary, das es
unter Windows nicht gibt: `cryptography` kann PKCS#12 selbst.

LAUFZEIT: EIN JAHR, SELBST ERNEUERT (seit 0.2.8)
-------------------------------------------------
Bis 0.2.7 galt das Zertifikat zehn Jahre — ein kopierter Schlüssel wäre so
lange brauchbar gewesen. Jetzt ein Jahr; ab 30 Tagen vor Ablauf erneuert
`erneuern()` es selbst per Graph `addKey`/`removeKey`. Dafür braucht die App
KEIN Graph-Recht (Primärquelle: learn.microsoft.com/graph/api/application-addkey,
„An application doesn't need any specific permission to roll its own keys"),
nur einen Nachweis, signiert mit dem noch gültigen Schlüssel. Ist der schon
abgelaufen, geht es nur noch über eine Admin-Anmeldung im Assistenten —
deshalb rechtzeitig, und deshalb sichtbar (`AUTH_CERT_LAUF`).

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


LAUFZEIT_TAGE = 365
ERNEUERN_AB_TAGEN = 30


def erzeugen(tage: int = LAUFZEIT_TAGE) -> dict:
    """Neues Schlüsselpaar schreiben. In Entra steht es danach NICHT — das tut
    der Assistent (`setup_wizard._upload_key_credential`) oder der Betreiber
    von Hand. Deshalb ist die gemerkte Schlüsselkennung danach ungültig."""
    _, _, pfx = _bauen(tage)
    secure_io.write_secret_bytes(PFX_PATH, pfx)
    import settings_store
    settings_store.update({"AUTH_KEY_ID": "", "AUTH_KEY_ALT": {}})
    log.info("Auth-Zertifikat erzeugt: %s (gültig %d Tage)", PFX_PATH, tage)
    return info()


def _bauen(tage: int):
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
    return key, cert, pfx


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
    # Ein importiertes Zertifikat gehört womöglich zur App des Signatur-Gateways:
    # deren Rechte und Schlüssel fasst das Relay nicht an (`rechte.eigene_app`).
    import settings_store
    settings_store.update({"APP_EIGEN": False, "AUTH_KEY_ID": "", "AUTH_KEY_ALT": {}})
    log.info("Auth-Zertifikat importiert: %s", PFX_PATH)
    return info()


# ── Erneuern ─────────────────────────────────────────────────────────────────

def _b64url(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def beweis(key, cert, objekt_id: str, jetzt: float | None = None) -> str:
    """Nachweis für addKey/removeKey: JWT, signiert mit einem gültigen Schlüssel
    der App. `iss` ist die OBJEKT-ID der App (nicht die Client-ID) — so im
    Beispielcode der Primärquelle (learn.microsoft.com/graph/application-rollkey-prooftoken);
    Laufzeit 10 Minuten, ohne `=`-Auffüllung (sonst Authentication_MissingOrMalformed —
    das erledigt PyJWT)."""
    import time
    import jwt
    from cryptography.hazmat.primitives import hashes
    t = int(jetzt if jetzt is not None else time.time())
    fp = cert.fingerprint(hashes.SHA1())
    return jwt.encode({"aud": "00000002-0000-0000-c000-000000000000", "iss": objekt_id,
                       "nbf": t, "exp": t + 600},
                      key, algorithm="RS256", headers={"x5t": _b64url(fp), "kid": fp.hex().upper()})


def _graph_token(key, cert) -> str:
    import msal
    import settings_store
    from cryptography.hazmat.primitives import hashes, serialization
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    app = msal.ConfidentialClientApplication(
        settings_store.get("CLIENT_ID").strip(),
        authority=f"https://login.microsoftonline.com/{settings_store.get('TENANT_ID').strip()}",
        client_credential={"private_key": pem, "thumbprint": cert.fingerprint(hashes.SHA1()).hex()})
    r = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in r:
        raise RuntimeError(f"Anmeldung der App bei Graph fehlgeschlagen: "
                           f"{r.get('error')}: {(r.get('error_description') or '')[:200]}")
    return r["access_token"]


def _graph_post(token: str, pfad: str, body: dict) -> dict:
    import httpx
    r = httpx.post(f"https://graph.microsoft.com/v1.0{pfad}", json=body, timeout=30,
                   headers={"Authorization": f"Bearer {token}"})
    if not r.is_success:
        raise RuntimeError(f"Graph {pfad} → {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def _merke(ok: bool, text: str) -> dict:
    import settings_store
    lauf = {"zeit": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ok": bool(ok), "text": str(text)[:400]}
    settings_store.update({"AUTH_CERT_LAUF": lauf})
    (log.info if ok else log.warning)("Auth-Zertifikat: %s", text)
    return lauf


def erneuern(erzwingen: bool = False) -> dict:
    """Ab `ERNEUERN_AB_TAGEN` vor Ablauf: neues Schlüsselpaar in Entra eintragen
    (addKey), lokal tauschen, alten Schlüssel austragen (removeKey).

    Reihenfolge mit Absicht: Lokal getauscht wird erst, wenn Entra den neuen
    Schlüssel KENNT. Scheitert addKey, bleibt alles beim Alten.

    ⚠️ removeKey gleich danach scheitert (live 07.10.2026: 401
    „Access Token missing or malformed" — so meldet Graph einen ungültigen
    Nachweis): Der Nachweis ist mit dem NEUEN Schlüssel signiert, und den kennt
    Entra Sekunden nach addKey noch nicht überall. Derselbe Aufruf 20 Minuten
    später: 204. Deshalb wird der alte Schlüssel als `AUTH_KEY_ALT` gemerkt und
    in den stündlichen Läufen ausgetragen (`_alten_austragen`); erst wenn das
    einen Tag lang nicht gelingt, ist es ein Fehler."""
    import settings_store
    from cryptography.hazmat.primitives import serialization
    if not settings_store.get("APP_EIGEN"):
        return {"ok": True, "aktion": "uebersprungen",
                "text": "Keine eigene App (importiertes Zertifikat) — Erneuerung beim Eigentümer."}
    objekt_id = (settings_store.get("APP_OBJECT_ID") or "").strip()
    if not PFX_PATH.exists() or not objekt_id or not (settings_store.get("TENANT_ID") or "").strip():
        return {"ok": False, "aktion": "uebersprungen",
                "text": "Zertifikat, Objekt-ID der App oder Tenant fehlt — einmal im Assistenten anmelden."}
    if settings_store.get("AUTH_KEY_ALT"):
        return _alten_austragen(objekt_id)
    alt_key, alt_cert = _laden()
    rest = alt_cert.not_valid_after_utc - _dt.datetime.now(_dt.timezone.utc)
    if not erzwingen and rest > _dt.timedelta(days=ERNEUERN_AB_TAGEN):
        return {"ok": True, "aktion": "nicht faellig",
                "text": f"gültig bis {alt_cert.not_valid_after_utc:%d.%m.%Y}, Erneuerung ab "
                        f"{(alt_cert.not_valid_after_utc - _dt.timedelta(days=ERNEUERN_AB_TAGEN)):%d.%m.%Y}"}
    if rest <= _dt.timedelta(0):
        return {"aktion": "abgelaufen", **_merke(False, "Zertifikat abgelaufen — Selbsterneuerung "
                "nicht mehr möglich. Im Assistenten neu anmelden (erzeugt und hinterlegt ein neues).")}
    try:
        token = _graph_token(alt_key, alt_cert)
        neu_key, neu_cert, neu_pfx = _bauen(LAUFZEIT_TAGE)
        # ⚠️ OHNE keyId: addKey lehnt eine mitgeschickte ab („KeyId value must be
        # null", live am 07.10.2026) — anders als PATCH beim ersten Hochladen.
        # Die Kennung vergibt Graph und liefert sie in der Antwort.
        antwort = _graph_post(token, f"/applications/{objekt_id}/addKey", {
            "keyCredential": {"type": "AsymmetricX509Cert", "usage": "Verify",
                              "key": _b64std(neu_cert.public_bytes(serialization.Encoding.DER))},
            "passwordCredential": None,
            "proof": beweis(alt_key, alt_cert, objekt_id)})
    except Exception as exc:                                  # noqa: BLE001
        return {"aktion": "fehler", **_merke(False, f"Erneuerung fehlgeschlagen, altes Zertifikat "
                                                    f"bleibt in Gebrauch: {exc}")}
    alt_id = (settings_store.get("AUTH_KEY_ID") or "").strip()
    secure_io.write_secret_bytes(PFX_PATH, neu_pfx)
    settings_store.update({"AUTH_KEY_ID": (antwort.get("keyId") or "").strip()})
    if not alt_id:
        text = ("erneuert; der alte Schlüssel ist dem Relay nicht bekannt und bleibt bis "
                f"{alt_cert.not_valid_after_utc:%d.%m.%Y} gültig (in Entra von Hand entfernen)")
        return {"aktion": "erneuert", **_merke(True, text)}
    settings_store.update({"AUTH_KEY_ALT": {
        "key_id": alt_id, "seit": _jetzt_text(),
        "bis": alt_cert.not_valid_after_utc.strftime("%Y-%m-%dT%H:%M:%SZ")}})
    return {"aktion": "erneuert", **_merke(True, f"erneuert, gültig bis "
            f"{neu_cert.not_valid_after_utc:%d.%m.%Y}; der alte Schlüssel wird in der nächsten "
            "Stunde ausgetragen (Entra muss den neuen erst überall kennen)")}


AUSTRAGEN_FRIST = _dt.timedelta(hours=24)


def _alten_austragen(objekt_id: str) -> dict:
    """Den gemerkten alten Schlüssel austragen; Nachweis mit dem aktuellen.
    „No credentials found" heißt: schon weg (von Hand entfernt) — erledigt."""
    import settings_store
    alt = settings_store.get("AUTH_KEY_ALT") or {}
    key, cert = _laden()
    try:
        _graph_post(_graph_token(key, cert), f"/applications/{objekt_id}/removeKey",
                    {"keyId": alt.get("key_id"), "proof": beweis(key, cert, objekt_id)})
    except Exception as exc:                                  # noqa: BLE001
        if "No credentials found" not in str(exc):
            try:
                seit = _dt.datetime.strptime(alt.get("seit", ""), "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=_dt.timezone.utc)
            except ValueError:
                seit = _dt.datetime.now(_dt.timezone.utc) - AUSTRAGEN_FRIST
            text = f"alter Schlüssel noch eingetragen (gültig bis {alt.get('bis', '?')[:10]}): {exc}"
            if _dt.datetime.now(_dt.timezone.utc) - seit < AUSTRAGEN_FRIST:
                log.info("Auth-Zertifikat: %s — nächster Versuch in einer Stunde", text)
                return {"ok": True, "aktion": "austragen ausstehend", "text": text}
            return {"aktion": "austragen", **_merke(False, text)}
    settings_store.update({"AUTH_KEY_ALT": {}})
    return {"aktion": "ausgetragen", **_merke(True, f"erneuert, gültig bis "
            f"{cert.not_valid_after_utc:%d.%m.%Y}; alter Schlüssel ausgetragen")}


def _jetzt_text() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64std(b: bytes) -> str:
    import base64
    return base64.b64encode(b).decode()
