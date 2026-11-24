"""Mit dem Auslieferungs-Kennwort lässt sich nur das Kennwort ändern (v0.2.25,
wie Gateway v1.10.113).

Bis v0.2.24 meldete `admin` (Vorgabe von WEBUI_PASSWORD) voll an. Das
Kopfbanner wies darauf hin, gesperrt war nichts.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import login_drossel  # noqa: E402
from webui import deps  # noqa: E402
from webui.app import app  # noqa: E402

VORGABE = ("admin", "admin")
NEU = "neues-kennwort-123"


@pytest.fixture
def stand(monkeypatch):
    gespeichert = {"SETUP_COMPLETE": True, "SESSION_SECRET": "testgeheimnis-vorgabe"}
    monkeypatch.setattr(deps.config, "WEBUI_PASSWORD", "admin")
    monkeypatch.setattr(deps.settings_store, "get", lambda k, *a, **kw: gespeichert.get(k))
    monkeypatch.setattr(deps.settings_store, "update", lambda d: gespeichert.update(d))
    monkeypatch.setattr(login_drossel, "_FEHLER", {})
    gesichert = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    yield gespeichert
    app.dependency_overrides.update(gesichert)


@pytest.fixture
def client():
    return TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


def test_api_mit_vorgabe_kennwort_gesperrt(stand, client):
    r = client.get("/api/relay/liste", auth=VORGABE)
    assert r.status_code == 403 and "Auslieferungs-Kennwort" in r.json()["detail"]


def test_seite_mit_vorgabe_kennwort_fuehrt_zu_schritt_1(stand, client):
    r = client.get("/relay", auth=VORGABE, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/einrichtung#step-1"


def test_sitzung_aus_dem_anmeldeformular_ist_ebenso_gesperrt(stand, client):
    r = client.post("/auth/local", json=dict(zip(("username", "password"), VORGABE)))
    assert r.status_code == 200 and r.json()["password_change_required"]
    assert client.get("/api/relay/liste").status_code == 403
    assert client.get("/einrichtung").status_code == 200, "Schritt 1 bleibt erreichbar"


def test_kennwort_aendern_bleibt_erreichbar_und_hebt_die_sperre_auf(stand, client):
    r = client.post("/api/password", auth=VORGABE, json={"current": "admin", "new": NEU})
    assert r.status_code == 200, r.text
    assert stand.get("ADMIN_PASSWORD_HASH")
    assert client.get("/api/relay/liste", auth=VORGABE).status_code == 401
    assert client.get("/api/relay/liste", auth=("admin", NEU)).status_code == 200


def test_eigenes_kennwort_aus_der_umgebung_sperrt_nicht(stand, client, monkeypatch):
    monkeypatch.setattr(deps.config, "WEBUI_PASSWORD", "eigenes-kennwort-1")
    assert client.get("/api/relay/liste", auth=("admin", "eigenes-kennwort-1")).status_code == 200


def test_microsoft_rueckruf_ist_mit_vorgabe_kennwort_gesperrt(stand, client, monkeypatch):
    """`/auth/callback` prüft die Sitzung selbst, nicht über `_check_auth`."""
    from webui.routen import einrichtung
    gelaufen = []

    async def nach_login(code, state):
        gelaufen.append(code)
    monkeypatch.setattr(einrichtung, "_nach_login", nach_login)
    client.post("/auth/local", json=dict(zip(("username", "password"), VORGABE)))
    r = client.get("/auth/callback", params={"code": "c", "state": "s"})
    assert "Auslieferungs-Kennwort" in r.text and not gelaufen
