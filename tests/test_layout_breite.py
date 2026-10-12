"""Dashboard und Geräteseite passen bei 1280 px, ohne umzubrechen oder abzuschneiden.

ANLASS (2026-10-13): Bei 1280 px brach die Kachel „davon mit TLS" den Wert
„25 (78 %)" dreizeilig um (36px-Schrift in einer Sechstel-Kachel von 960px),
und die Gerätetabellen (min-width 820–980px) liefen in `main` mit 960px
Höchstbreite über — die letzte Spalte stand abgeschnitten im Rollbereich.

Zwei Prüfungen:
- statisch (läuft immer): breite Seiten setzen `main_klasse` = weit, der
  Anteil steht nicht im Kachelwert;
- gemessen im Browser (nur mit Playwright + Chromium, sonst übersprungen).
"""
import asyncio
import re
from pathlib import Path

import pytest

VORLAGEN = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"
BREITE_SEITEN = ("dashboard.html", "relay.html")


@pytest.mark.parametrize("name", BREITE_SEITEN)
def test_seiten_mit_breiten_tabellen_sind_weit(name):
    text = (VORLAGEN / name).read_text(encoding="utf-8")
    assert "{% block main_klasse %}weit{% endblock %}" in text, name


def test_jede_breite_tabelle_liegt_auf_einer_weiten_seite():
    """Gegenrichtung: eine neue Seite mit breiter Tabelle muss in BREITE_SEITEN."""
    for p in VORLAGEN.glob("*.html"):
        if re.search(r"<table[^>]*min-width:\s*(\d+)px", p.read_text(encoding="utf-8")):
            breiten = [int(b) for b in re.findall(r"<table[^>]*min-width:\s*(\d+)px",
                                                   p.read_text(encoding="utf-8"))]
            if max(breiten) >= 700:
                assert p.name in BREITE_SEITEN, f"{p.name}: Tabelle {max(breiten)}px, Seite nicht weit"


def test_tls_anteil_steht_nicht_im_kachelwert():
    text = (VORLAGEN / "dashboard.html").read_text(encoding="utf-8")
    zeile = next(z for z in text.splitlines() if "getElementById('k-tls').textContent" in z)
    assert "%" not in zeile, zeile


# ── Messung im Browser ───────────────────────────────────────────────────────

class _Sitzung:
    def __init__(self, ip, tls):
        self.peer = (ip, 1)
        self.ssl = {"cipher": ("x",)} if tls else None


class _Umschlag:
    def __init__(self, empfaenger):
        self.mail_from, self.rcpt_tos = "drucker@firma.de", list(empfaenger)
        self.content = b"Subject: Scan\r\n\r\ny"


@pytest.fixture
def anlage(einstellungen, monkeypatch, tmp_path):
    import auth_cert
    import exo_mailboxes
    import handler
    import mail_audit
    import relay_hosts
    import smarthost
    einstellungen["SETUP_COMPLETE"] = True
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda: {"chefin@firma.de"})
    monkeypatch.setattr(relay_hosts, "DB_PATH", tmp_path / "relay_hosts.db")
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    monkeypatch.setattr(mail_audit, "_initialised", False)
    monkeypatch.setattr(auth_cert, "PFX_PATH", tmp_path / "auth.pfx")
    mail_audit.init_db()
    monkeypatch.setattr(smarthost, "send", lambda *a, **kw: None)
    for i, name in enumerate(("Kopierer 2. OG Buchhaltung", "Scanner Empfang",
                              "Warenwirtschaft", "USV-Monitoring", "Zeiterfassung")):
        relay_hosts.speichern(f"10.1.5.3{i}", kommentar=name, extern=(i % 2 == 0))
    # 32 Einlieferungen, 25 mit TLS → „25 (78 %)": zweistellig plus Anteil.
    for k in range(32):
        asyncio.run(handler.RelayHandler().handle_DATA(
            None, _Sitzung(f"10.1.5.3{k % 5}", k < 25), _Umschlag(["chefin@firma.de"])))
    relay_hosts.merke_abweisung("192.168.77.200", "unbekannt@lieferant-mit-langem-namen.de",
                                ["chefin@firma.de"], "Gerät nicht in der Liste")


_MESSUNG = """() => ({
  werte: [...document.querySelectorAll('.stat-value')].map(e => [e.id, e.textContent,
          Math.round(e.getBoundingClientRect().height),
          parseFloat(getComputedStyle(e).lineHeight) || 1.2 * parseFloat(getComputedStyle(e).fontSize)]),
  tabellen: [...document.querySelectorAll('table')].map(t => [
          t.querySelector('th') ? t.querySelector('th').textContent : '?',
          t.scrollWidth, t.parentElement.clientWidth]),
  seite: [document.documentElement.scrollWidth, innerWidth],
})"""


def _messen(pfad: str, breite: int, warte_js: str):
    sync_api = pytest.importorskip("playwright.sync_api")
    from fastapi.testclient import TestClient
    from webui.app import app
    from webui.deps import _check_auth
    app.dependency_overrides[_check_auth] = lambda: "test"
    c = TestClient(app, base_url="https://testserver")

    def weiter(route):
        r = c.get(route.request.url.replace("https://testserver", ""))
        route.fulfill(status=r.status_code, body=r.content,
                      headers={"content-type": r.headers.get("content-type", "text/html")})
    try:
        with sync_api.sync_playwright() as p:
            try:
                b = p.chromium.launch()
            except Exception as exc:                  # Chromium nicht installiert
                pytest.skip(f"Chromium fehlt: {exc}")
            seite = b.new_page(viewport={"width": breite, "height": 900}, device_scale_factor=1.5)
            seite.route("https://testserver/**", weiter)
            seite.goto("https://testserver" + pfad)
            seite.wait_for_function(warte_js)
            m = seite.evaluate(_MESSUNG)
            b.close()
            return m
    finally:
        app.dependency_overrides.clear()


_SEITEN = {
    "/": "document.getElementById('k-tls').textContent !== '–' && !document.body.textContent.includes('Wird geladen')",
    "/relay": "!document.body.textContent.includes('Wird geladen')",
}


@pytest.mark.parametrize("pfad", list(_SEITEN))
def test_bei_1280_bricht_nichts_um_und_nichts_wird_abgeschnitten(anlage, pfad):
    m = _messen(pfad, 1280, _SEITEN[pfad])
    if pfad == "/":
        assert any(w[0] == "k-tls" and w[1] == "25" for w in m["werte"]), m["werte"]
    for wid, text, hoehe, zeile in m["werte"]:
        assert hoehe < 1.5 * zeile, f"Kachel {wid} „{text}“ bricht um ({hoehe}px bei Zeile {zeile}px)"
    assert m["tabellen"], "keine Tabelle gefunden — Messung prüft nichts"
    for kopf, breit, platz in m["tabellen"]:
        assert breit <= platz, f"Tabelle ab „{kopf}“: {breit}px in {platz}px — rechts abgeschnitten"
    assert m["seite"][0] <= m["seite"][1], m["seite"]


@pytest.mark.parametrize("pfad", list(_SEITEN))
def test_schmal_kein_seitenueberlauf_und_kein_umbruch_in_der_zahl(anlage, pfad):
    m = _messen(pfad, 393, _SEITEN[pfad])
    for wid, text, hoehe, zeile in m["werte"]:
        assert hoehe < 1.5 * zeile, f"Kachel {wid} „{text}“ bricht um"
    assert m["seite"][0] <= m["seite"][1], f"Seite läuft waagerecht über: {m['seite']}"
