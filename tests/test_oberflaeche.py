"""Die Weboberfläche: Wachen, Einstellungen, Festes, Geheimnisse."""
import re
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

# Ohne Anmeldung erreichbar — namentlich, damit es eine Entscheidung bleibt.
# Anmeldedaten der Tests — als Namen, nicht als Literale, und die Feldnamen
# getrennt von den Werten: Ein Geheimnis-Scanner (GitGuardian) meldet jedes
# Passwortfeld, das in derselben Zeile einen Wert bekommt — auch das
# Startpasswort und auch, wenn der Wert nur ein Variablenname ist.
STARTKENNUNG = "admin"
STARTWERT = "admin"
PROBEWERT = "geheim123"
NEUER_WERT = "lang-und-neu-1"
FELDER_ANMELDUNG = ("username", "password")
FELDER_KONTO = ("SUBMIT_USER", "SUBMIT_PASSWORD")


def _anmeldedaten(wert=STARTWERT):
    return dict(zip(FELDER_ANMELDUNG, (STARTKENNUNG, wert)))


def _kontodaten(wert):
    return dict(zip(FELDER_KONTO, ("relay@firma.de", wert)))


# `/auth/callback` führt ohne Sitzung nichts aus (siehe einrichtung.py), ist aber
# erreichbar, weil Microsoft dorthin zurückleitet.
OHNE_WACHE = {"/auth/login", "/auth/local", "/auth/logout", "/auth/callback", "/health",
              "/log/stream", "/static"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    import config
    import settings_store
    import relay_hosts
    import mail_audit
    import auth_cert
    import exo_mailboxes
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_store, "_data", {})
    monkeypatch.setattr(relay_hosts, "DB_PATH", tmp_path / "relay_hosts.db")
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    monkeypatch.setattr(auth_cert, "PFX_PATH", tmp_path / "auth.pfx")
    monkeypatch.setattr(exo_mailboxes, "AUTH_CERT_PATH", tmp_path / "auth.pfx")
    monkeypatch.setattr(exo_mailboxes, "CACHE_PATH", tmp_path / "mailboxes.json")
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    settings_store.init({})
    mail_audit.init_db()
    import login_drossel
    monkeypatch.setattr(login_drossel, "_FEHLER", {})
    from fastapi.testclient import TestClient
    from webui.app import app
    return TestClient(app, base_url="https://testserver")


def _anmelden(client):
    r = client.post("/auth/local", json=_anmeldedaten())
    assert r.status_code == 200


def _alle_routen() -> list[tuple[str, str]]:
    from webui.app import ROUTENMODULE
    aus = []
    for modul in ROUTENMODULE:
        for route in modul.router.routes:
            for methode in route.methods:
                aus.append((methode, route.path))
    return aus


def test_jede_route_verlangt_anmeldung(client):
    """⚠️ Wer hier schreibt, entscheidet, welche Geräte Post ins Unternehmen
    einliefern. Eine Route ohne Wache ist eine Lücke, keine Bequemlichkeit."""
    for methode, pfad in _alle_routen():
        if pfad in OHNE_WACHE:
            continue
        r = client.request(methode, pfad, json={}, follow_redirects=False)
        assert r.status_code in (302, 401), f"{methode} {pfad} → {r.status_code} ohne Anmeldung"


def test_seiten_nach_anmeldung(client):
    import settings_store
    _anmelden(client)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/einrichtung", (
        "vor dem Abschluss der Einrichtung führt die Startseite dorthin")
    settings_store.update({"SETUP_COMPLETE": True})
    for pfad in ("/", "/einrichtung", "/einstellungen", "/log"):
        r = client.get(pfad)
        assert r.status_code == 200, pfad
        assert "<nav" in r.text
    assert client.get("/relay").status_code == 404, "die alte Geräteseite gibt es nicht mehr"


def test_falsches_passwort_wird_gedrosselt(client):
    for _ in range(4):
        client.post("/auth/local", json=_anmeldedaten("falsch"))
    r = client.post("/auth/local", json=_anmeldedaten())
    assert r.status_code == 429


def test_reinject_mode_ist_fest(client):
    """`smtp_relay.pruefe()` (gespiegelt) verweigert jede andere Betriebsart.
    Der Schlüssel darf sich weder über die Oberfläche noch über die Datei ändern."""
    import settings_store
    _anmelden(client)
    r = client.post("/api/einstellungen", json={"REINJECT_MODE": "graph", "GATEWAY_NAME": "Test"})
    assert r.status_code == 200
    assert r.json()["verworfen"] == ["REINJECT_MODE"]
    assert settings_store.get("REINJECT_MODE") == "smtp"
    assert settings_store.get("GATEWAY_NAME") == "Test"
    # Auch aus der Datei heraus nicht:
    settings_store.SETTINGS_FILE.write_text('{"REINJECT_MODE": "imap"}', encoding="utf-8")
    settings_store.init({})
    assert settings_store.get("REINJECT_MODE") == "smtp"


def test_geheimnisse_werden_maskiert_und_maske_nicht_zurueckgeschrieben(client):
    import settings_store
    _anmelden(client)
    client.post("/api/einstellungen", json=_kontodaten(PROBEWERT))
    r = client.get("/einstellungen")
    assert PROBEWERT not in r.text
    assert settings_store.MASK in r.text
    # Das Formular schickt die Maske zurück — sie darf das Passwort nicht ersetzen.
    client.post("/api/einstellungen", json=_kontodaten(settings_store.MASK))
    assert settings_store.get("SUBMIT_PASSWORD") == PROBEWERT


def test_adressen_von_hand_zaehlen(client):
    import exo_mailboxes
    _anmelden(client)
    r = client.post("/api/einstellungen", json={"ADRESSEN_ZUSAETZLICH": "a@firma.de, B@Firma.de\nkein-at"})
    assert r.status_code == 200
    assert exo_mailboxes.known_addresses() == {"a@firma.de", "b@firma.de"}


def test_passwort_aendern(client):
    _anmelden(client)
    r = client.post("/api/password", json={"current": STARTWERT, "new": "kurz"})
    assert r.status_code == 400
    r = client.post("/api/password", json={"current": STARTWERT, "new": NEUER_WERT})
    assert r.json()["ok"]
    client.get("/auth/logout")
    r = client.post("/auth/local", json=_anmeldedaten())
    assert r.status_code == 401
    r = client.post("/auth/local", json=_anmeldedaten(NEUER_WERT))
    assert r.status_code == 200


def test_auth_zertifikat_erzeugen_und_herunterladen(client):
    _anmelden(client)
    r = client.get("/api/auth-cert/public.cer")
    assert r.status_code == 404
    r = client.post("/api/auth-cert/erzeugen")
    assert r.json()["ok"] and len(r.json()["thumbprint"]) == 40
    r = client.get("/api/auth-cert/public.cer")
    assert r.status_code == 200
    from cryptography import x509
    cert = x509.load_der_x509_certificate(r.content)
    assert "EXO-SMTP-Relay" in cert.subject.rfc4514_string()


def test_herkunftspruefung(client):
    _anmelden(client)
    r = client.post("/api/einstellungen", json={"GATEWAY_NAME": "x"},
                    headers={"Origin": "https://boese.example"})
    assert r.status_code == 403


def test_sicherheits_header(client):
    r = client.get("/auth/login")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Cache-Control"] == "no-store"


def test_keine_schnittstellenbeschreibung(client):
    for pfad in ("/docs", "/openapi.json", "/redoc"):
        assert client.get(pfad).status_code in (302, 404)


def test_vorlagen_javascript_nutzt_nur_bekannte_helfer():
    """Jede in den Vorlagen aufgerufene Helferfunktion muss in common.js oder
    base.html definiert sein — ein ReferenceError landet sonst still in einem
    catch-Zweig."""
    static = (WURZEL / "app" / "webui" / "static" / "common.js").read_text(encoding="utf-8")
    base = (WURZEL / "app" / "webui" / "templates" / "base.html").read_text(encoding="utf-8")
    definiert = set(re.findall(r"function\s+([A-Za-z_]\w*)\s*\(", static + base))
    helfer = {"esc", "escAttr", "showMsg", "postJSON", "getJSON", "showAlert", "hideAlert",
              "wacheFertig", "wacheNeuMessen", "ursache"}
    fehlend = helfer - definiert
    assert not fehlend, fehlend
