"""Betriebszustände als Band auf jeder Seite (v0.2.34, wie Gateway 5bd5b1b).

„Relay abgeschaltet“ und „keine Postfachadressen“ standen bis dahin nur in der
Übersicht, Zustellfehler nur als Zahl in einer Kachel. Geprüft:
1. die Bänder kommen aus base.html (Jinja-Globals), nicht mehr aus Übersicht
   oder Geräteseite,
2. die Anmeldeseite zeigt keinen Betriebszustand,
3. Zustellfehler zählen nur seit der letzten erfolgreichen Zustellung,
4. vor abgeschlossener Einrichtung gibt es keine Bänder.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from webui import deps

VORLAGEN = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"
KENNUNGEN = ("relay-aus-banner", "adressen-banner", "zustellfehler-banner")


def _seite(monkeypatch, active="dashboard", aus=False, adressen=False, fehler=0, lern=0):
    g = deps.templates.env.globals
    monkeypatch.setitem(g, "relay_aus_gemeldet", lambda: aus)
    monkeypatch.setitem(g, "adressen_gemeldet", lambda: adressen)
    monkeypatch.setitem(g, "zustellfehler_anzahl", lambda: fehler)
    monkeypatch.setitem(g, "lernmodus_rest_min", lambda: lern)
    monkeypatch.setitem(g, "password_change_required", lambda: False)
    req = SimpleNamespace(url=SimpleNamespace(path="/"))
    return deps.templates.env.get_template("base.html").render(request=req, active=active)


def test_ohne_befund_kein_band(monkeypatch):
    html = _seite(monkeypatch)
    for kennung in KENNUNGEN:
        assert f'id="{kennung}"' not in html


def test_baender_erscheinen_mit_zahlen_und_ziel(monkeypatch):
    html = _seite(monkeypatch, aus=True, adressen=True, fehler=1)
    assert 'id="relay-aus-banner"' in html and 'href="/einstellungen#relay"' in html
    assert 'id="adressen-banner"' in html and 'href="/einstellungen#adressen"' in html
    assert 'id="zustellfehler-banner"' in html and "1 Einlieferung konnte" in html
    assert 'href="/einstellungen#rueckweg"' in html
    assert "3 Einlieferungen konnten" in _seite(monkeypatch, fehler=3)


def test_farben_abgeschaltet_gelb_fehler_rot(monkeypatch):
    html = _seite(monkeypatch, aus=True, adressen=True, fehler=2)

    def stil(kennung):
        i = html.index(f'id="{kennung}"')
        return html[i:html.index(">", i)]
    assert "#fffbeb" in stil("relay-aus-banner")
    assert "#fef2f2" in stil("adressen-banner") and "#fef2f2" in stil("zustellfehler-banner")


def test_anmeldeseite_zeigt_keinen_betriebszustand(monkeypatch):
    """Wer nicht angemeldet ist, erfährt nichts — auch nicht den Lernmodus,
    der bis v0.2.33 auf der Anmeldeseite stand."""
    html = _seite(monkeypatch, active="login", aus=True, adressen=True, fehler=1, lern=5)
    for kennung in KENNUNGEN + ("lernmodus-banner",):
        assert kennung not in html, kennung


def test_uebersicht_und_geraeteseite_haben_keinen_eigenen_weg_mehr():
    """Ein zweiter Weg neben base.html — genau das wurde abgelöst."""
    dash = (VORLAGEN / "dashboard.html").read_text("utf-8")
    assert "r.relay_an" not in dash and "r.adressen === 0" not in dash
    relay = (VORLAGEN / "relay.html").read_text("utf-8")
    assert "relay-warnung" not in relay and "r.relay_an" not in relay


# ── Datenquellen ─────────────────────────────────────────────────────────────

@pytest.fixture
def werte(monkeypatch):
    import settings_store
    w = {"SETUP_COMPLETE": True, "SMTP_RELAY_ENABLED": True, "ADRESSEN_ZUSAETZLICH": []}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: w.get(k, d))
    return w


def test_relay_aus_erst_nach_der_einrichtung(werte):
    assert deps._relay_aus_gemeldet() is False
    werte["SMTP_RELAY_ENABLED"] = False
    assert deps._relay_aus_gemeldet() is True
    werte["SETUP_COMPLETE"] = False
    assert deps._relay_aus_gemeldet() is False


def test_adressen_band(werte, monkeypatch):
    import exo_mailboxes
    bekannt = set()
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda: bekannt)
    assert deps._adressen_gemeldet() is True
    bekannt.add("a@firma.de")
    assert deps._adressen_gemeldet() is False
    bekannt.clear()
    werte["SETUP_COMPLETE"] = False
    assert deps._adressen_gemeldet() is False


@pytest.fixture
def protokoll(monkeypatch, tmp_path):
    import mail_audit
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    monkeypatch.setattr(mail_audit, "_initialised", False)
    mail_audit.init_db()

    def ereignis(action, ts=None):
        mail_audit.log_event(sender="s@firma.de", recipients=["e@firma.de"], subject="",
                             message_id="", action=action, quelle="10.0.0.5")
        if ts:
            with mail_audit._conn() as c:
                c.execute("UPDATE mail_log SET ts=? WHERE id=(SELECT MAX(id) FROM mail_log)", (ts,))
    return ereignis


def test_zustellfehler_nur_seit_der_letzten_zustellung(protokoll):
    import mail_audit
    protokoll("relay_fehler")
    protokoll("relay_fehler")
    assert mail_audit.zustellfehler_offen() == 2
    protokoll("relay")                      # Rückweg wieder offen
    assert mail_audit.zustellfehler_offen() == 0
    protokoll("relay_fehler")
    protokoll("relay_abgelehnt")            # Abweisung ist kein Zustellfehler
    assert mail_audit.zustellfehler_offen() == 1


def test_zustellfehler_aelter_als_das_fenster_zaehlen_nicht(protokoll):
    import mail_audit
    protokoll("relay_fehler", ts="2000-01-01T00:00:00Z")
    assert mail_audit.zustellfehler_offen() == 0
    protokoll("relay_fehler")
    assert mail_audit.zustellfehler_offen() == 1


def test_band_auf_echter_seite(protokoll, monkeypatch):
    """Ende zu Ende über das Jinja-Global, nicht über einen Ersatz."""
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: d)
    protokoll("relay_fehler")
    req = SimpleNamespace(url=SimpleNamespace(path="/"))
    html = deps.templates.env.get_template("base.html").render(request=req, active="relay")
    assert 'id="zustellfehler-banner"' in html
