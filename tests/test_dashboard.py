"""Das Dashboard misst, was je Gerät geschah — TLS, Klartext, intern, extern."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


@pytest.fixture
def anlage(einstellungen, monkeypatch, tmp_path):
    import exo_mailboxes
    import mail_audit
    import relay_hosts
    import smarthost
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda: {"chefin@firma.de", "lager@firma.de"})
    monkeypatch.setattr(relay_hosts, "DB_PATH", tmp_path / "relay_hosts.db")
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    monkeypatch.setattr(mail_audit, "_initialised", False)
    mail_audit.init_db()
    monkeypatch.setattr(smarthost, "send", lambda *a, **kw: None)
    relay_hosts.speichern("10.1.5.30", kommentar="Kopierer", extern=True)
    relay_hosts.speichern("10.1.5.31", kommentar="Scanner")
    return einstellungen


class _Sitzung:
    def __init__(self, ip, ssl):
        self.peer = (ip, 1)
        self.ssl = ssl


class _Umschlag:
    def __init__(self, absender, empfaenger):
        self.mail_from, self.rcpt_tos = absender, list(empfaenger)
        self.content = b"Subject: x\r\n\r\ny"


def _lauf(ip, empfaenger=("chefin@firma.de",), absender="drucker@firma.de", tls=True):
    import handler
    return asyncio.run(handler.RelayHandler().handle_DATA(
        None, _Sitzung(ip, {"cipher": ("x",)} if tls else None), _Umschlag(absender, empfaenger)))


def test_auswertung_je_geraet(anlage):
    import mail_audit
    assert _lauf("10.1.5.30", tls=True).startswith("250")
    assert _lauf("10.1.5.30", tls=False).startswith("250")
    assert _lauf("10.1.5.30", empfaenger=("kunde@extern.example",), tls=True).startswith("250")
    assert _lauf("10.1.5.30", absender="spam@fremd.example").startswith("550")
    assert _lauf("10.1.5.31", tls=False).startswith("250")
    assert _lauf("10.1.5.31", empfaenger=("kunde@extern.example",)).startswith("550"), "31 darf nicht nach aussen"

    a = mail_audit.auswertung(30)
    k = a["quellen"]["10.1.5.30"]
    assert k == {"zugestellt": 3, "tls": 2, "klartext": 1, "intern": 2, "extern": 1, "abgelehnt": 1, "fehler": 0}
    s = a["quellen"]["10.1.5.31"]
    assert s == {"zugestellt": 1, "tls": 0, "klartext": 1, "intern": 1, "extern": 0, "abgelehnt": 1, "fehler": 0}
    assert a["gesamt"]["zugestellt"] == 4 and a["gesamt"]["extern"] == 1 and a["gesamt"]["abgelehnt"] == 2


def test_zustellfehler_zaehlt_getrennt(anlage, monkeypatch):
    import mail_audit
    import smarthost
    monkeypatch.setattr(smarthost, "send", lambda *a, **kw: (_ for _ in ()).throw(OSError("weg")))
    assert _lauf("10.1.5.30").startswith("451")
    k = mail_audit.auswertung(30)["quellen"]["10.1.5.30"]
    assert k["fehler"] == 1 and k["zugestellt"] == 0


def test_dashboard_api_verbindet_geraete_und_statistik(anlage, monkeypatch, tmp_path):
    import settings_store
    import auth_cert
    import login_drossel
    monkeypatch.setattr(login_drossel, "_FEHLER", {})
    monkeypatch.setattr(auth_cert, "PFX_PATH", tmp_path / "auth.pfx")
    _lauf("10.1.5.30", tls=False)
    from fastapi.testclient import TestClient
    from webui.app import app
    from webui.deps import _check_auth
    app.dependency_overrides[_check_auth] = lambda: "test"
    try:
        c = TestClient(app, base_url="https://testserver")
        r = c.get("/api/dashboard?tage=7").json()
    finally:
        app.dependency_overrides.clear()
    assert r["ok"] and r["tage"] == 7
    kopierer = next(g for g in r["geraete"] if g["ip"] == "10.1.5.30")
    assert kopierer["statistik"]["klartext"] == 1
    assert kopierer["tls"] == "nein"
    assert r["gesamt"]["zugestellt"] == 1
    assert r["ereignisse"][0]["tls"] == 0 and r["ereignisse"][0]["extern"] == 0
