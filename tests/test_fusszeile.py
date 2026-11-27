"""„Über“-Fußzeile (0.2.58-beta): auf JEDER HTML-Seite, aus EINER Vorlage.

Vorlage ist die Fußzeile von exogateway.zarenko.net (Herausgeber, Logo,
Impressum, Datenschutz) — ohne Support-Mail. Die Erwartungen stehen hier
unabhängig von der Vorlage: Version aus der Datei VERSION (nicht aus
config.VERSION), Links als feste Liste, Seiten aus den Routen selbst (eine
neue Seite ohne Fußzeile wird rot, ohne dass jemand diesen Test anfasst).
"""
import re
from pathlib import Path

from fastapi.responses import HTMLResponse

from test_oberflaeche import _anmelden, client  # noqa: F401  (Fixture)

WURZEL = Path(__file__).resolve().parent.parent
VORLAGEN = WURZEL / "app" / "webui" / "templates"
VERSION = (WURZEL / "VERSION").read_text(encoding="utf-8").strip()

LINKS = {
    "https://exogateway.zarenko.net/smtp-relay/",
    "https://github.com/azitc-ac/exo-smtp-relay",
    "https://blog.zarenko.net",
    "https://github.com/azitc-ac/exo-smtp-relay/blob/main/LICENSE.md",
    "https://exogateway.zarenko.net/impressum.html",
    "https://exogateway.zarenko.net/datenschutz.html",
}


def _fusszeile(html: str) -> str:
    treffer = re.findall(r'<footer id="fusszeile">(.*?)</footer>', html, re.S)
    assert len(treffer) == 1, f"Fußzeile {len(treffer)}-mal statt genau einmal"
    return treffer[0]


def _pruefen(html: str, wo: str) -> None:
    f = _fusszeile(html)
    text = re.sub(r"<[^>]+>", " ", f)
    text = re.sub(r"\s+", " ", text)
    assert "Alexander Zarenko – IT Consulting" in text, wo
    assert f"EXO SMTP Relay v{VERSION}" in text, f"{wo}: Version {VERSION} fehlt in »{text.strip()}«"
    assert set(re.findall(r'href="([^"]+)"', f)) == LINKS, wo
    assert "Apache-2.0" in text and "Impressum" in text and "Datenschutz" in text, wo
    # Keine Support-Mail, kein externer Abruf (offline, CSP img-src 'self').
    assert "mailto:" not in f and "@" not in text, f"{wo}: Mailadresse in der Fußzeile"
    quellen = re.findall(r'src="([^"]+)"', f)
    assert quellen and all(q.startswith("/static/") for q in quellen), f"{wo}: {quellen}"


def _html_routen() -> list[str]:
    from webui.app import ROUTENMODULE
    return sorted({r.path for m in ROUTENMODULE for r in m.router.routes
                   if "GET" in getattr(r, "methods", ()) and getattr(r, "response_class", None) is HTMLResponse
                   and "{" not in r.path})


def test_fusszeile_auf_jeder_seite(client):
    import settings_store
    routen = _html_routen()
    assert {"/", "/relay", "/einrichtung", "/einstellungen", "/backup", "/aktualisierung",
            "/log", "/auth/login", "/auth/callback"} <= set(routen), routen
    # Ohne Anmeldung: Login und Callback (öffentliche Seiten tragen das Impressum gerade).
    _pruefen(client.get("/auth/login").text, "/auth/login")
    _pruefen(client.get("/auth/callback?error=x&error_description=y").text, "/auth/callback")
    _anmelden(client)
    settings_store.update({"SETUP_COMPLETE": True})
    geprueft = {"/auth/login", "/auth/callback"}
    for pfad in routen:
        if pfad in geprueft:
            continue
        r = client.get(pfad, follow_redirects=False)
        assert r.status_code == 200, f"{pfad} → {r.status_code}"
        _pruefen(r.text, pfad)
        geprueft.add(pfad)
    assert geprueft == set(routen)


def test_jede_seitenvorlage_erbt_base():
    """Der gemeinsame Pfad: Seiten erben base.html, Teilstücke beginnen mit „_“."""
    for p in VORLAGEN.glob("*.html"):
        if p.name.startswith("_") or p.name == "base.html":
            continue
        assert p.read_text(encoding="utf-8").lstrip().startswith('{% extends "base.html" %}'), p.name
    assert VORLAGEN.joinpath("base.html").read_text(encoding="utf-8").count('{% include "_fusszeile.html" %}') == 1


def test_logo_lokal_ausgeliefert(client):
    r = client.get("/static/azitc-logo.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.lstrip().startswith("<svg") and "http" not in r.text.replace("http://www.w3.org/2000/svg", ""), (
        "Logo lädt etwas nach — es muss offline vollständig sein")


def test_logo_im_paket():
    import update_paket
    im_paket = {rel for _f, rel in update_paket._dateien(WURZEL)}
    assert "app/webui/static/azitc-logo.svg" in im_paket
    assert "app/webui/templates/_fusszeile.html" in im_paket
