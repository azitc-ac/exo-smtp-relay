"""Fenster nur über fensterImKlick()/anmeldeFenster() aus common.js öffnen.

Nach einem `await` gilt der Klick nicht mehr als Nutzergeste, und der Browser
blockiert das Fenster stumm. Die Einrichtung öffnete die Microsoft-Anmeldung erst
nach der Serverantwort. Gegenstück zur gleichnamigen Regel im Gateway.
"""
from pathlib import Path

WEBUI = Path(__file__).parent.parent / "app/webui"


def test_window_open_nur_in_common_js():
    treffer = []
    for p in sorted(WEBUI.rglob("*")):
        if p.suffix not in (".html", ".js") or "vendor" in p.parts or p.name == "common.js":
            continue
        for nr, zeile in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "window.open(" in zeile:
                treffer.append(f"{p.relative_to(WEBUI)}:{nr}")
    assert not treffer, treffer


def test_einrichtung_nutzt_anmeldefenster():
    t = (WEBUI / "templates/einrichtung.html").read_text(encoding="utf-8")
    assert "anmeldeFenster(async function" in t
