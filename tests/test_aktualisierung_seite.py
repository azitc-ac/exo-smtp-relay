"""Aktualisierungsseite: Verfolgen eines Einspielens im Browser (0.2.60-beta).

ANLASS: Alex, Update 0.2.56 → 0.2.58-beta auf Windows. Der Tausch lief durch (Protokoll ok,
„data/ unverändert"), die Seite stand aber dauerhaft auf „Dienst startet neu …" — nach dem Neuladen
von Hand war alles gut. Bis 0.2.59 zeigte JEDER gescheiterte Abruf diesen Text, ohne Zeitlimit:
kein Netz, 401, eine Anmeldeseite statt JSON, 5xx.

Geprüft wird im echten Chromium (Playwright; fehlt es, übersprungen) gegen eine nachgestellte
/api/aktualisierung, deren Antworten der Test je Abruf vorgibt. Takt und Fristen setzt der Test
über window.AKT_TEST herunter.
"""
import json
import time

import pytest

ALT, NEU = "0.2.58-beta", "0.2.60-beta"


def _zustand(aktuell=ALT, zustand="laeuft", text="Tausch läuft"):
    return {"ok": True, "aktuell": aktuell, "betriebsart": "windows", "bereit": True, "hinweis": "",
            "offline": False, "ruecknahme_offen": False, "paket": "exo-smtp-relay-<neu>-windows.zip",
            "schluessel": True, "kanal": "beta", "intervall": 0,
            "stand": {"neueste": NEU, "verfuegbar": True},
            "status": {"zustand": zustand, "text": text, "zeit": "2026-11-27T10:00:00Z"},
            "docker": {"image": "x", "befehl": "x"}, "zeit": 0}


ANMELDESEITE = (200, "text/html; charset=utf-8", "<!doctype html><title>Anmelden</title><form>")
NETZ_WEG = None


def _json(d, status=200):
    return (status, "application/json", json.dumps(d))


def _ablauf(monkeypatch, antworten, warte_auf, frist_s=8):
    """Seite laden (Zustand laeuft → Verfolgen startet), /api/aktualisierung je Abruf aus
    `antworten` (letzte wiederholt sich). Rückgabe: Ergebnis von `warte_auf` und die Zahl der
    Seitenaufrufe (2 = neu geladen)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    import aktualisierung
    from fastapi.testclient import TestClient
    from webui.app import app
    from webui.deps import _check_auth
    monkeypatch.setattr(aktualisierung, "zustand", lambda: _zustand())
    app.dependency_overrides[_check_auth] = lambda: "test"
    c = TestClient(app, base_url="https://testserver")
    zaehler = {"api": 0, "seite": 0}

    def weiter(route):
        url = route.request.url.replace("https://testserver", "")
        if url.split("?")[0] == "/api/aktualisierung":
            a = antworten[min(zaehler["api"], len(antworten) - 1)]
            zaehler["api"] += 1
            if a is NETZ_WEG:
                route.abort("connectionrefused")
            else:
                route.fulfill(status=a[0], body=a[2], headers={"content-type": a[1]})
            return
        if url.split("?")[0] == "/aktualisierung":
            zaehler["seite"] += 1
        r = c.get(url)
        route.fulfill(status=r.status_code, body=r.content,
                      headers={"content-type": r.headers.get("content-type", "text/html")})
    try:
        with sync_api.sync_playwright() as p:
            try:
                b = p.chromium.launch()
            except Exception as exc:                  # Chromium nicht installiert
                pytest.skip(f"Chromium fehlt: {exc}")
            seite = b.new_page(viewport={"width": 393, "height": 800})
            seite.add_init_script("window.AKT_TEST = {takt: 100, frist: 1500, laufFrist: 3000};")
            seite.route("https://testserver/**", weiter)
            seite.goto("https://testserver/aktualisierung")
            ende = time.time() + frist_s
            ergebnis = None
            while time.time() < ende:
                try:
                    ergebnis = seite.evaluate(warte_auf)
                except Exception:                      # Navigation (Neuladen) läuft gerade
                    ergebnis = None
                if ergebnis or zaehler["seite"] >= 2:
                    break
                seite.wait_for_timeout(100)
            status = ""
            try:
                status = seite.evaluate("document.getElementById('status').textContent")
            except Exception:
                pass
            b.close()
            return ergebnis, zaehler["seite"], status
    finally:
        app.dependency_overrides.clear()


_DIALOG = """() => { const d = document.getElementById('akt-dialog');
  return d && getComputedStyle(d).display !== 'none' ? d.textContent.replace(/\\s+/g, ' ') : null; }"""
_FEHLER = """() => { const s = document.getElementById('status');
  return s && s.classList.contains('error') ? s.textContent.replace(/\\s+/g, ' ') : null; }"""


def test_neue_fassung_meldet_sich_dialog_mit_versionen(monkeypatch):
    """Erst kein Netz, dann die NEUE Fassung — auch wenn deren Status noch „laeuft" sagt
    (Nachbereitung): Abschluss über den Versionsvergleich, Dialog „vALT → vNEU"."""
    dialog, seiten, _ = _ablauf(monkeypatch, [NETZ_WEG, NETZ_WEG, _json(_zustand(NEU, "laeuft", "Nachbereitung"))],
                                _DIALOG)
    assert dialog and "Update erfolgreich" in dialog and f"v{ALT} → v{NEU}" in dialog, dialog
    assert seiten == 1, "ohne Klick auf „Neu laden“ wird nicht neu geladen (wie Gateway)"


def test_anmeldeseite_statt_json_heisst_dienst_ist_da_neu_laden(monkeypatch):
    _, seiten, status = _ablauf(monkeypatch, [NETZ_WEG, ANMELDESEITE], "() => null")
    assert seiten >= 2, f"nicht neu geladen, Status: {status!r}"


def test_401_heisst_dienst_ist_da_neu_laden(monkeypatch):
    _, seiten, status = _ablauf(monkeypatch, [NETZ_WEG, _json({"detail": "Nicht angemeldet"}, 401)], "() => null")
    assert seiten >= 2, f"nicht neu geladen, Status: {status!r}"


def test_dauerhaft_kein_netz_endet_mit_fehlermeldung_und_protokollpfad(monkeypatch):
    fehler, seiten, _ = _ablauf(monkeypatch, [NETZ_WEG], _FEHLER)
    assert fehler and "nicht erreichbar" in fehler and "app.log" in fehler and "neu laden" in fehler, fehler
    assert seiten == 1


def test_dauerhaft_http_500_zeigt_code_und_text(monkeypatch):
    fehler, _, _ = _ablauf(monkeypatch, [_json({"detail": "Status unlesbar"}, 500)], _FEHLER)
    assert fehler and "HTTP 500" in fehler and "Status unlesbar" in fehler, fehler


def test_zurueckgerollt_ist_ein_fehler_kein_erfolg(monkeypatch):
    fehler, _, _ = _ablauf(monkeypatch, [_json(_zustand(ALT, "zurueckgerollt", "neue Fassung kam nicht hoch"))],
                           _FEHLER)
    assert fehler and "kam nicht hoch" in fehler, fehler


def test_api_behaelt_die_felder_die_auch_aeltere_seiten_lesen():
    """Die Seite einer ÄLTEREN Fassung pollt nach dem Tausch gegen die NEUE API. Sie liest
    ok, aktuell, status.zustand, status.text — fällt eines weg, hängt sie wieder."""
    import inspect
    import aktualisierung
    quelle = inspect.getsource(aktualisierung.zustand)
    for feld in ('"ok": True', '"aktuell": config.VERSION', '"status": status()'):
        assert feld in quelle, feld


def test_sitzung_ueberlebt_einen_neustart(einstellungen):
    """Klärt eine Vermutung zum Hänger: Die Sitzung hängt NICHT am Prozess. Das Geheimnis steht in
    den Einstellungen (data/), das Merkmal ist signiert und zustandslos — ein Keks von vor dem
    Neustart gilt danach weiter (bis SESSION_TTL oder Kennwortwechsel)."""
    import importlib
    import sitzung
    keks = sitzung.create_session_cookie("admin")
    assert einstellungen.get("SESSION_SECRET"), "Geheimnis nicht in den Einstellungen abgelegt"
    sitzung = importlib.reload(sitzung)                  # neuer Prozess: nichts im Speicher
    assert (sitzung.verify_session_cookie(keks) or {}).get("u") == "admin"


def test_logseite_laedt_neu_wenn_das_stream_token_nach_neustart_ungueltig_ist():
    """Geschwister (Suchlauf 0.2.60-beta): Die Log-Seite stand nach einem Neustart ewig auf
    „Verbinde…“, weil das Stream-Token nur im Speicher des alten Prozesses lebte. /log/stream
    lehnt ab, /health antwortet → die Seite muss neu laden (neues Token)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    from fastapi.testclient import TestClient
    from webui.app import app
    from webui.deps import _check_auth
    app.dependency_overrides[_check_auth] = lambda: "test"
    c = TestClient(app, base_url="https://testserver")
    zaehler = {"seite": 0, "stream": 0}

    def weiter(route):
        pfad = route.request.url.replace("https://testserver", "").split("?")[0]
        if pfad == "/log/stream":
            zaehler["stream"] += 1
            route.fulfill(status=401, body='{"detail":"Invalid or expired token"}',
                          headers={"content-type": "application/json"})
            return
        if pfad == "/log":
            zaehler["seite"] += 1
        r = c.get(route.request.url.replace("https://testserver", ""))
        route.fulfill(status=r.status_code, body=r.content,
                      headers={"content-type": r.headers.get("content-type", "text/html")})
    try:
        with sync_api.sync_playwright() as p:
            try:
                b = p.chromium.launch()
            except Exception as exc:
                pytest.skip(f"Chromium fehlt: {exc}")
            seite = b.new_page()
            seite.add_init_script("window.LOG_TEST = {takt: 100};")
            seite.route("https://testserver/**", weiter)
            seite.goto("https://testserver/log")
            ende = time.time() + 8
            while time.time() < ende and zaehler["seite"] < 2:
                seite.wait_for_timeout(100)
            b.close()
    finally:
        app.dependency_overrides.clear()
    assert zaehler["stream"] >= 2 and zaehler["seite"] >= 2, zaehler
