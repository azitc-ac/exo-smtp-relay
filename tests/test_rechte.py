"""Rechte und Schlüssel der App: herabstufen, nur die eigene App, Selbsterneuerung.

ANLASS (07.10.2026): Die App hatte dauerhaft Exchange-Administrator und einen
Zehn-Jahres-Schlüssel. Jetzt: Admin nur für den Connector, danach Lesen; der
Schlüssel gilt ein Jahr und erneuert sich selbst.

Was hier geprüft wird, sind die Stellen, an denen ein Fehler STILL bliebe:
die Reihenfolge (nie die Admin-Rolle entfernen, bevor das Lesen gesichert ist;
nie lokal tauschen, bevor Entra den neuen Schlüssel kennt), die Grenze „nur
die eigene App" (die mitbenutzte App des Gateways nie herabstufen) und der
Nachweis-Token, dessen Form Microsoft vorgibt. Gegen Exchange und Graph selbst
wurde am 07.10.2026 live geprüft — hier stehen Attrappen dafür.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import auth_cert  # noqa: E402
import rechte  # noqa: E402
import settings_store  # noqa: E402


@pytest.fixture
def werte(monkeypatch, tmp_path):
    w = {"CLIENT_ID": "11111111-1111-1111-1111-111111111111", "APP_EIGEN": True,
         "APP_SP_ID": "22222222-2222-2222-2222-222222222222",
         "APP_OBJECT_ID": "33333333-3333-3333-3333-333333333333",
         "TENANT_ID": "44444444-4444-4444-4444-444444444444",
         "TENANT_DOMAIN": "firma.onmicrosoft.com", "GATEWAY_NAME": "EXO SMTP Relay",
         "AUTH_KEY_ID": "alt-key-id"}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: w.get(k))
    monkeypatch.setattr(settings_store, "update", lambda d: w.update(d))
    monkeypatch.setattr(auth_cert, "PFX_PATH", tmp_path / "auth.pfx")
    rechte.vergiss_admin_token()
    return w


# ── Herabstufen ──────────────────────────────────────────────────────────────

def _herab_mit(monkeypatch, gruppe_ok: bool):
    aufrufe = []
    monkeypatch.setattr(rechte, "lesegruppe_einrichten",
                        lambda: aufrufe.append("lesegruppe") or {"ok": gruppe_ok, "text": "x"})

    async def entfernen(token):
        aufrufe.append(("entfernen", token))
        return 1
    monkeypatch.setattr(rechte, "adminrolle_entfernen", entfernen)
    return aufrufe


def test_herabstufen_sichert_erst_das_lesen_dann_entfernt_es_admin(werte, monkeypatch):
    aufrufe = _herab_mit(monkeypatch, gruppe_ok=True)
    lauf = asyncio.run(rechte.herabstufen("tok"))
    assert aufrufe == ["lesegruppe", ("entfernen", "tok")]
    assert lauf["ok"] and lauf["stufe"] == "lesen"


def test_scheitert_die_lesegruppe_bleibt_admin_stehen(werte, monkeypatch):
    """Sonst stünde die App ohne jedes Recht da — und könnte die Gruppe mangels
    Rechten auch nicht mehr anlegen."""
    aufrufe = _herab_mit(monkeypatch, gruppe_ok=False)
    lauf = asyncio.run(rechte.herabstufen("tok"))
    assert aufrufe == ["lesegruppe"], "Admin-Rolle entfernt, obwohl das Lesen nicht gesichert war"
    assert not lauf["ok"] and lauf["stufe"] == "admin"


def test_ohne_admin_token_wird_nichts_veraendert_und_es_steht_da(werte, monkeypatch):
    aufrufe = _herab_mit(monkeypatch, gruppe_ok=True)
    lauf = asyncio.run(rechte.herabstufen())
    assert aufrufe == []
    assert not lauf["ok"] and "anmelden" in lauf["text"].lower()
    assert werte["RECHTE_LAUF"]["ok"] is False, "der Zustand muss sichtbar gemerkt sein"


def test_fremde_app_wird_nie_herabgestuft(werte, monkeypatch):
    """Importiertes Zertifikat = womöglich die App des Signatur-Gateways, die im
    Betrieb Schreibrechte braucht."""
    werte["APP_EIGEN"] = False
    aufrufe = _herab_mit(monkeypatch, gruppe_ok=True)
    lauf = asyncio.run(rechte.herabstufen("tok"))
    assert aufrufe == [] and not lauf["ok"]


def test_importieren_markiert_die_app_als_fremd(werte, tmp_path):
    key, cert, pfx = auth_cert._bauen(1)
    auth_cert.importieren(pfx, "")
    assert werte["APP_EIGEN"] is False and werte["AUTH_KEY_ID"] == ""


def test_admin_token_verfaellt_vor_seinem_ablauf(werte):
    rechte.merke_admin_token("tok", 3600)
    assert rechte.admin_token() == "tok"
    rechte.merke_admin_token("tok", 200)          # weniger als der Puffer
    assert rechte.admin_token() is None


# ── Nachweis-Token (Form von Microsoft vorgegeben) ───────────────────────────

def test_beweis_hat_die_vorgegebene_form():
    import jwt
    from cryptography.hazmat.primitives import hashes
    key, cert, _ = auth_cert._bauen(1)
    t = 1_800_000_000
    tok = auth_cert.beweis(key, cert, "obj-id", jetzt=t)
    kopf, rumpf, _sig = tok.split(".")
    assert "=" not in kopf + rumpf, "Auffüllung im JWT → Authentication_MissingOrMalformed"
    claims = jwt.decode(tok, cert.public_key(), algorithms=["RS256"],
                        audience="00000002-0000-0000-c000-000000000000",
                        options={"verify_exp": False, "verify_nbf": False})
    assert claims["iss"] == "obj-id"
    assert claims["exp"] - claims["nbf"] == 600
    h = jwt.get_unverified_header(tok)
    fp = cert.fingerprint(hashes.SHA1())
    assert h["x5t"] == base64.urlsafe_b64encode(fp).rstrip(b"=").decode()


# ── Erneuern ─────────────────────────────────────────────────────────────────

def _altes_zertifikat(tage_rest: float):
    """Ein PFX, das in *tage_rest* Tagen abläuft."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "alt")])
    j = dt.datetime.now(dt.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(n).issuer_name(n).public_key(k.public_key())
         .serial_number(1).not_valid_before(j - dt.timedelta(days=300))
         .not_valid_after(j + dt.timedelta(days=tage_rest)).sign(k, hashes.SHA256()))
    auth_cert.PFX_PATH.write_bytes(pkcs12.serialize_key_and_certificates(
        b"alt", k, c, None, serialization.NoEncryption()))
    return k, c


def _graph(monkeypatch, add_fehler=False):
    import jwt
    aufrufe = []
    monkeypatch.setattr(auth_cert, "_graph_token", lambda key, cert: "tok")

    def post(token, pfad, body):
        aufrufe.append((pfad.rsplit("/", 1)[-1], body))
        if pfad.endswith("addKey"):
            # Wie Graph live (07.10.2026): eine mitgeschickte keyId ist ein Fehler,
            # die Kennung vergibt der Server. Die frühere Attrappe gab die gesendete
            # einfach zurück — und verdeckte genau diesen Fehler.
            if add_fehler:
                raise RuntimeError("400 proof invalid")
            if body["keyCredential"].get("keyId") is not None:
                raise RuntimeError('400 {"message":"KeyId value must be null."}')
            return {"keyId": "server-vergeben-1234"}
        return {}
    monkeypatch.setattr(auth_cert, "_graph_post", post)
    return aufrufe, jwt


def test_nicht_faellig_kein_netz(werte, monkeypatch):
    _altes_zertifikat(200)
    aufrufe, _ = _graph(monkeypatch)
    r = auth_cert.erneuern()
    assert r["aktion"] == "nicht faellig" and aufrufe == []


def test_erneuern_reihenfolge_und_nachweise(werte, monkeypatch):
    alt_key, alt_cert = _altes_zertifikat(10)
    vorher = auth_cert.PFX_PATH.read_bytes()
    aufrufe, jwt = _graph(monkeypatch)
    r = auth_cert.erneuern()
    assert r["aktion"] == "erneuert" and r["ok"], r
    # removeKey NICHT im selben Lauf: Entra kennt den neuen Schlüssel Sekunden
    # nach addKey noch nicht (live 07.10.2026: 401, 20 Minuten später 204).
    assert [a[0] for a in aufrufe] == ["addKey"]
    jwt.decode(aufrufe[0][1]["proof"], alt_cert.public_key(), algorithms=["RS256"],
               audience="00000002-0000-0000-c000-000000000000")
    assert auth_cert.PFX_PATH.read_bytes() != vorher
    assert werte["AUTH_KEY_ID"] == "server-vergeben-1234"
    assert werte["AUTH_KEY_ALT"]["key_id"] == "alt-key-id", "alter Schlüssel nicht zum Austragen gemerkt"
    _, neu_cert = auth_cert._laden()
    rest = neu_cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
    assert dt.timedelta(days=360) < rest <= dt.timedelta(days=366)

    # Nächster stündlicher Lauf: austragen, Nachweis mit dem NEUEN Schlüssel
    aufrufe.clear()
    r = auth_cert.erneuern()
    assert r["aktion"] == "ausgetragen" and r["ok"], r
    assert [a[0] for a in aufrufe] == ["removeKey"]
    assert aufrufe[0][1]["keyId"] == "alt-key-id"
    jwt.decode(aufrufe[0][1]["proof"], neu_cert.public_key(), algorithms=["RS256"],
               audience="00000002-0000-0000-c000-000000000000")
    assert not werte["AUTH_KEY_ALT"]


def _ausstehend(werte, monkeypatch, fehler: str, alter_h: float):
    _altes_zertifikat(300)
    seit = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=alter_h)
    werte["AUTH_KEY_ALT"] = {"key_id": "alt-key-id", "seit": seit.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "bis": "2026-10-17T00:00:00Z"}
    monkeypatch.setattr(auth_cert, "_graph_token", lambda key, cert: "tok")

    def post(token, pfad, body):
        raise RuntimeError(fehler)
    monkeypatch.setattr(auth_cert, "_graph_post", post)
    return auth_cert.erneuern()


def test_austragen_scheitert_zunaechst_still_und_bleibt_gemerkt(werte, monkeypatch):
    r = _ausstehend(werte, monkeypatch, "401 Access Token missing or malformed", alter_h=1)
    assert r["ok"] and r["aktion"] == "austragen ausstehend"
    assert werte["AUTH_KEY_ALT"]["key_id"] == "alt-key-id"


def test_austragen_nach_einem_tag_ist_ein_sichtbarer_fehler(werte, monkeypatch):
    r = _ausstehend(werte, monkeypatch, "401 Access Token missing or malformed", alter_h=25)
    assert not r["ok"] and werte["AUTH_CERT_LAUF"]["ok"] is False
    assert werte["AUTH_KEY_ALT"], "gemerkter Schlüssel darf beim Fehler nicht verloren gehen"


def test_schon_entfernter_schluessel_gilt_als_ausgetragen(werte, monkeypatch):
    r = _ausstehend(werte, monkeypatch, "404 No credentials found to be removed", alter_h=1)
    assert r["ok"] and r["aktion"] == "ausgetragen" and not werte["AUTH_KEY_ALT"]


def test_neuer_schluessel_von_hand_vergisst_den_auszutragenden(werte):
    werte["AUTH_KEY_ALT"] = {"key_id": "alt-key-id"}
    auth_cert.erzeugen(1)
    assert werte["AUTH_KEY_ALT"] == {} and werte["AUTH_KEY_ID"] == ""


def test_scheitert_addkey_bleibt_der_alte_schluessel(werte, monkeypatch):
    _altes_zertifikat(10)
    vorher = auth_cert.PFX_PATH.read_bytes()
    aufrufe, _ = _graph(monkeypatch, add_fehler=True)
    r = auth_cert.erneuern()
    assert not r["ok"]
    assert auth_cert.PFX_PATH.read_bytes() == vorher, "lokal getauscht, obwohl Entra den neuen nicht kennt"
    assert werte["AUTH_KEY_ID"] == "alt-key-id"
    assert [a[0] for a in aufrufe] == ["addKey"]


def test_fremde_app_wird_nicht_erneuert(werte, monkeypatch):
    werte["APP_EIGEN"] = False
    _altes_zertifikat(10)
    aufrufe, _ = _graph(monkeypatch)
    assert auth_cert.erneuern()["aktion"] == "uebersprungen" and aufrufe == []


def test_abgelaufen_meldet_statt_zu_versuchen(werte, monkeypatch):
    _altes_zertifikat(-1)
    aufrufe, _ = _graph(monkeypatch)
    r = auth_cert.erneuern()
    assert r["aktion"] == "abgelaufen" and not r["ok"] and aufrufe == []


# ── Nachmessen: Exchange übernimmt das Herabstufen verzögert ─────────────────
# Live 07.10.2026: 3 h 20 min nach dem Herabstufen schrieb die App noch. Ohne
# Nachmessung stünde in der Oberfläche „herabgestuft", während ein kopierter
# Schlüssel weiter Administrator ist.

def _messung(monkeypatch, schreib: str):
    import exo_setup
    monkeypatch.setattr(exo_setup, "_voraussetzungen", lambda: (True, ""))
    aufrufe = []

    def ps(body, timeout=300):
        aufrufe.append(body)
        return {"ok": True, "output": f"RECHTE schreib={schreib} lesen=True\n"}
    monkeypatch.setattr(rechte, "_ps", ps)
    return aufrufe


def test_nachmessen_haelt_fest_ab_wann_es_wirkt(werte, monkeypatch):
    t0 = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=200)
    werte["RECHTE_HERABGESTUFT"] = t0.strftime("%Y-%m-%dT%H:%M:%SZ")
    _messung(monkeypatch, "New-InboundConnector")
    assert rechte.nachmessen()["stufe"] == "admin"
    assert not werte.get("RECHTE_WIRKSAM"), "Schreibrecht gemessen, aber als wirksam vermerkt"
    assert werte["RECHTE_MESSUNG"]["stufe"] == "admin"

    _messung(monkeypatch, "")
    assert rechte.nachmessen()["stufe"] == "lesen"
    assert 199 <= werte["RECHTE_WIRKSAM"]["minuten"] <= 201

    aufrufe = _messung(monkeypatch, "")
    assert rechte.nachmessen() is None and aufrufe == [], "misst weiter, obwohl es schon wirkt"


def test_ohne_herabstufen_wird_nicht_nachgemessen(werte, monkeypatch):
    aufrufe = _messung(monkeypatch, "")
    assert rechte.nachmessen() is None and aufrufe == []


def test_messknopf_vor_dem_herabstufen_setzt_nichts_wirksam(werte, monkeypatch):
    _messung(monkeypatch, "")
    rechte.messen()
    assert not werte.get("RECHTE_WIRKSAM")


def test_hochstufen_beendet_die_beobachtung(werte, monkeypatch):
    werte.update({"RECHTE_HERABGESTUFT": "2026-10-07T00:10:12Z",
                  "RECHTE_WIRKSAM": {"zeit": "x", "minuten": 5}})

    async def keine(token):
        return []

    async def gh(*a, **kw):
        return {}
    monkeypatch.setattr(rechte, "_zuweisungen", keine)
    monkeypatch.setattr(rechte, "_gh", gh)
    asyncio.run(rechte.adminrolle_zuweisen("tok"))
    assert werte["RECHTE_HERABGESTUFT"] == "" and werte["RECHTE_WIRKSAM"] == {}


def test_herabstufen_beginnt_die_beobachtung_neu(werte, monkeypatch):
    werte["RECHTE_WIRKSAM"] = {"zeit": "alt", "minuten": 5}
    _herab_mit(monkeypatch, gruppe_ok=True)
    asyncio.run(rechte.herabstufen("tok"))
    assert werte["RECHTE_WIRKSAM"] == {} and werte["RECHTE_HERABGESTUFT"]
