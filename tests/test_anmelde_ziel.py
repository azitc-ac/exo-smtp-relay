"""Ziel nach der Anmeldung: nur Pfade auf diesem Server (wie Gateway v1.9.138).

Bis v0.2.23 setzte login.html `next` aus der Adresszeile ungeprüft in
`location.href` (ein `javascript:`-Ziel liefe mit der frischen Sitzung), und
eine schon angemeldete Sitzung wurde ungeprüft auf `next` weitergeleitet
(`//fremd.example` liest der Browser als fremden Host).
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from webui.routen import anmeldung  # noqa: E402


@pytest.mark.parametrize("ziel", ["/", "/relay", "/einstellungen?x=1#a", "/log"])
def test_eigene_pfade_bleiben(ziel):
    assert anmeldung._sicheres_ziel(ziel) == ziel


@pytest.mark.parametrize("ziel", [
    "//fremd.example", "/\\fremd.example", "javascript:alert(1)", "https://fremd.example",
    "fremd.example", "", None, "/a b", "/a\tb", "/a\nb", "/a\x00b", 42,
])
def test_fremde_ziele_werden_zu_wurzel(ziel):
    assert anmeldung._sicheres_ziel(ziel) == "/"


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from webui.app import app
    return TestClient(app, base_url="https://testserver")


def test_anmeldeseite_bekommt_geprueftes_ziel(client):
    seite = client.get("/auth/login", params={"next": "javascript:alert(1)"}).text
    assert 'const ANMELDE_ZIEL = "/";' in seite
    assert "javascript:alert" not in seite
    seite = client.get("/auth/login", params={"next": "/relay"}).text
    assert 'const ANMELDE_ZIEL = "/relay";' in seite


def test_angemeldete_sitzung_wird_nicht_nach_draussen_geleitet(client, monkeypatch):
    monkeypatch.setattr(anmeldung, "_get_session_user", lambda request: "admin")
    r = client.get("/auth/login", params={"next": "//fremd.example"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"
    r = client.get("/auth/login", params={"next": "/relay"}, follow_redirects=False)
    assert r.headers["location"] == "/relay"


def test_anmeldeseite_liest_ziel_nicht_selbst_aus_der_adresszeile():
    js = (ROOT / "app/webui/templates/login.html").read_text(encoding="utf-8")
    assert not re.search(r"location\.search\)\.get\(['\"]next", js)
    assert "window.location.href = ANMELDE_ZIEL" in js


def test_jede_weiterleitung_auf_next_nutzt_die_pruefung():
    src = (ROOT / "app/webui/routen/anmeldung.py").read_text(encoding="utf-8")
    weiterleitungen = re.findall(r"RedirectResponse\((?!\")(.+?),", src)
    assert weiterleitungen, "keine Weiterleitung auf ein variables Ziel gefunden"
    assert all(w.startswith("_sicheres_ziel(") for w in weiterleitungen), weiterleitungen
