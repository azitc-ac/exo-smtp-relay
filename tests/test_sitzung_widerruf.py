"""Sitzungen enden mit dem Kennwortwechsel (v0.2.26, wie Gateway v1.11.3).

Bis v0.2.25 galt ein Sitzungsmerkmal seine vollen 8 Stunden, auch nachdem das
Kennwort geändert war.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import settings_store  # noqa: E402
import sitzung  # noqa: E402


@pytest.fixture
def stand(monkeypatch):
    werte = {"ADMIN_PASSWORD_HASH": "pbkdf2:sha256:alt", "SESSION_SECRET": "testgeheimnis-widerruf"}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    monkeypatch.setattr(settings_store, "update", lambda d: werte.update(d))
    return werte


def test_kennwortwechsel_beendet_sitzungen(stand):
    keks = sitzung.create_session_cookie("admin")
    assert sitzung.verify_session_cookie(keks)
    stand["ADMIN_PASSWORD_HASH"] = "pbkdf2:sha256:neu"
    assert sitzung.verify_session_cookie(keks) is None
    assert sitzung.verify_session_cookie(sitzung.create_session_cookie("admin"))


def test_wechsel_ueber_die_umgebung_beendet_sitzungen(stand, monkeypatch):
    import config
    stand["ADMIN_PASSWORD_HASH"] = ""
    monkeypatch.setattr(config, "WEBUI_PASSWORD", "erstes-kennwort-1")
    keks = sitzung.create_session_cookie("admin")
    assert sitzung.verify_session_cookie(keks)
    monkeypatch.setattr(config, "WEBUI_PASSWORD", "zweites-kennwort-2")
    assert sitzung.verify_session_cookie(keks) is None


def test_alte_merkmale_ohne_kennung_gelten_nicht(stand):
    """Merkmale von vor v0.2.26 tragen keine Kennung — einmal neu anmelden."""
    from itsdangerous import URLSafeTimedSerializer
    alt = URLSafeTimedSerializer("testgeheimnis-widerruf").dumps({"u": "admin", "ts": 0})
    assert sitzung.verify_session_cookie(alt) is None


def test_kennwort_aendern_gibt_der_eigenen_sitzung_ein_neues_merkmal(stand, monkeypatch):
    from fastapi.testclient import TestClient
    from webui.routen import anmeldung
    from webui.app import app
    monkeypatch.setattr(anmeldung, "_check_password", lambda pw: pw == "altes-kennwort")
    gesichert = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        c = TestClient(app, base_url="https://testserver")
        alt = sitzung.create_session_cookie("admin")
        c.cookies.set(sitzung.SESSION_COOKIE, alt)
        r = c.post("/api/password", json={"current": "altes-kennwort", "new": "neues-kennwort-1"})
        assert r.status_code == 200, r.text
        assert sitzung.verify_session_cookie(alt) is None, "das alte Merkmal ist ungültig"
        neu = r.cookies.get(sitzung.SESSION_COOKIE)
        assert neu and sitzung.verify_session_cookie(neu), "die eigene Sitzung läuft weiter"
    finally:
        app.dependency_overrides.update(gesichert)
