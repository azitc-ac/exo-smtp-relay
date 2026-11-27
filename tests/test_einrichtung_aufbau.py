"""Aufbau der Einrichtungskarten: genau EIN Primärknopf, und zwar am Ende.

Anlass (0.2.61-beta): In Schritt 4 stand „Connector anlegen“ mitten in der Karte, darunter
Postfachliste, Rechte und Schlüssel — Erledigtes unter der nächsten Aktion. Im Zustand
„Eingerichtet“ war „Connector nachziehen“ blau, das nötige „Anmelden und herabstufen“ grau:
Die App behielte so dauerhaft Exchange-Administrator.

Regel je Karte (.wizard-step), zustandsabhängig gerendert:
  * offene Karte mit Bedienelementen → genau ein Primärknopf; erledigte → höchstens einer;
  * nach dem Primärknopf kein weiteres Bedienelement und kein Erfolgskasten.
Ausgenommen: `data-fortsetzung` (Einfügefeld nach einem Login, erscheint erst auf Klick),
`data-alternative` (gleichwertiger anderer Weg, z. B. Gerät von Hand), und `data-baustein`
(eigenständiger Block mit eigener Ordnung — der Let's-Encrypt-Block, zählt als EIN Primärknopf).
"""
import re
from html.parser import HTMLParser

import pytest

from test_oberflaeche import _anmelden, client  # noqa: F401  (Fixture)

_LEER = {"input", "br", "img", "meta", "link", "hr", "source", "wbr"}
_BEDIEN = {"button", "input", "select", "textarea"}


class _Karten(HTMLParser):
    """Je Karte die Ereignisfolge: ('primaer', text) | ('bedien', tag) | ('erfolg', '')."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stapel = []          # (tag, attrs)
        self.karten = {}          # id -> {"done": bool, "folge": [...]}
        self._knopf = None

    def _innen(self, merkmal):
        return any(merkmal in a for _, a in self.stapel)

    def _karte(self):
        for tag, a in reversed(self.stapel):
            if "wizard-step" in (a.get("class") or "").split():
                return a.get("id")
        return None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        a = {k: (v if v is not None else "") for k, v in a.items()}
        if tag not in _LEER:
            self.stapel.append((tag, a))
        if "wizard-step" in (a.get("class") or "").split():
            self.karten[a["id"]] = {"done": "done" in a["class"].split(), "folge": []}
            return
        karte = self._karte()
        if not karte or self._innen("data-fortsetzung"):
            return
        folge = self.karten[karte]["folge"]
        klassen = (a.get("class") or "").split()
        alternativ = self._innen("data-alternative")
        if self._innen("data-baustein"):
            if tag == "button" and "primary" in klassen and not any(
                    e[0] == "primaer" and e[2] for e in folge):
                folge.append(("primaer", "[Baustein]", True, False))
            return
        if tag == "button" and "primary" in klassen:
            self._knopf = [("primaer", "", False, "disabled" in a)]
            folge.append(self._knopf)
        elif tag in _BEDIEN and a.get("type") != "hidden":
            folge.append(("bedien", tag + "#" + (a.get("id") or a.get("onclick") or ""), alternativ))
        elif tag == "div" and {"alert", "success"} <= set(klassen):
            folge.append(("erfolg", a.get("id") or "", alternativ))

    def handle_data(self, data):
        if self._knopf is not None:
            alt = self._knopf[0]
            self._knopf[0] = (alt[0], alt[1] + data, alt[2], alt[3])

    def handle_endtag(self, tag):
        if tag == "button":
            self._knopf = None
        for i in range(len(self.stapel) - 1, -1, -1):
            if self.stapel[i][0] == tag:
                del self.stapel[i:]
                break


def _karten(seite: str) -> dict:
    p = _Karten()
    p.feed(seite)
    aus = {}
    for kid, k in p.karten.items():
        folge = [e[0] if isinstance(e, list) else e for e in k["folge"]]
        aus[kid] = {"done": k["done"], "folge": folge}
    return aus


def _primaer(folge):
    return [(i, " ".join(e[1].split())) for i, e in enumerate(folge) if e[0] == "primaer" and not e[3]]


def _regel_pruefen(seite: str) -> dict:
    """Prüft die Regel für alle Karten; liefert je Karte den Text des Primärknopfs (oder None)."""
    ergebnis = {}
    for kid, k in _karten(seite).items():
        folge = k["folge"]
        prim = _primaer(folge)
        hat_bedien = any(e[0] in ("bedien", "primaer") for e in folge)
        if not k["done"] and hat_bedien:
            assert len(prim) == 1, f"{kid}: offene Karte braucht genau einen Primärknopf, hat {prim}"
        assert len(prim) <= 1, f"{kid}: mehr als ein Primärknopf: {prim}"
        if prim:
            danach = [e for e in folge[prim[0][0] + 1:] if e[0] in ("bedien", "erfolg") and not e[2]]
            assert not danach, f"{kid}: nach „{prim[0][1]}“ steht noch {danach}"
        ergebnis[kid] = prim[0][1] if prim else None
    return ergebnis


_GRUND = {"PUBLIC_HOSTNAME": "relay.firma.de"}
_APP = {"AZURE_APP_CREATED": True, "APP_EIGEN": True, "CLIENT_ID": "c", "APP_SP_ID": "s"}
_ADMIN_LAUF = {"RECHTE_LAUF": {"zeit": "2026-11-27T10:00:00Z", "ok": False, "stufe": "admin",
                               "text": "Admin-Anmeldung abgelaufen — die App hat weiter Exchange-Administrator."}}

ZUSTAENDE = {
    # name: (Einstellungen, admin_token, erwarteter Primärknopf in Schritt 4)
    "frisch": ({}, False, None),
    "app_angelegt": ({**_GRUND, **_APP}, False, "Connector anlegen"),
    "connector_admin_ohne_anmeldung": ({**_GRUND, **_APP, **_ADMIN_LAUF, "EXO_CONNECTOR_CREATED": True},
                                       False, "Anmelden und herabstufen"),
    "connector_admin_angemeldet": ({**_GRUND, **_APP, "EXO_CONNECTOR_CREATED": True},
                                   True, "Jetzt auf Lesen herabstufen"),
    "herabgestuft": ({**_GRUND, **_APP, "EXO_CONNECTOR_CREATED": True,
                      "RECHTE_HERABGESTUFT": "2026-11-27T10:00:00Z"}, False, None),
    "fremde_app": ({**_GRUND, "AZURE_APP_CREATED": True, "CLIENT_ID": "c", "EXO_CONNECTOR_CREATED": True},
                   False, None),
    "abgeschlossen": ({**_GRUND, **_APP, "EXO_CONNECTOR_CREATED": True, "SETUP_COMPLETE": True,
                       "RECHTE_HERABGESTUFT": "2026-11-27T10:00:00Z",
                       "RECHTE_WIRKSAM": {"zeit": "2026-11-27T11:00:00Z", "minuten": 60}}, False, None),
}


@pytest.mark.parametrize("name", list(ZUSTAENDE))
def test_genau_ein_primaerknopf_am_kartenende(client, monkeypatch, name):  # noqa: F811
    import rechte
    import settings_store
    werte, token, erwartet = ZUSTAENDE[name]
    _anmelden(client)
    settings_store.update(werte)
    monkeypatch.setattr(rechte, "admin_token", lambda: "tok" if token else None)
    seite = client.get("/einrichtung").text
    primaer = _regel_pruefen(seite)
    assert set(primaer) == {f"step-{i}" for i in range(1, 7)}, primaer
    assert primaer["step-4"] == erwartet, (name, primaer["step-4"])
    if werte.get("EXO_CONNECTOR_CREATED") and werte.get("APP_EIGEN"):
        assert "Connector nachziehen" in seite, "Nachziehen fehlt in der Pflege"
    if name == "connector_admin_ohne_anmeldung":
        # Nächster Schritt als Anleitung, nicht als Störung: kein gelber Kasten „abgelaufen“.
        block = seite.split('id="herabstufen-block"', 1)[1].split("<!-- ── 5", 1)[0]
        assert "Nächster Schritt: Rechte der App herabstufen" in block
        assert "Kurz anmelden, dann stuft das Relay herab." in block
        assert "Admin-Anmeldung abgelaufen" not in seite, "Normalfall erscheint als Störung"


def test_regel_erkennt_den_alten_aufbau():
    """Gegenprobe ohne Server: der Aufbau bis 0.2.60 (Pflege unter „Connector anlegen“,
    zwei Primärknöpfe) fällt durch."""
    alt = ('<div class="wizard-step" id="step-4"><div class="step-body">'
           '<button class="btn primary">Connector anlegen</button>'
           '<button class="btn secondary">Rechte jetzt messen</button></div></div>')
    with pytest.raises(AssertionError, match="nach „Connector anlegen“"):
        _regel_pruefen(alt)
    zwei = ('<div class="wizard-step done" id="step-4"><div class="step-body">'
            '<button class="btn primary">Connector nachziehen</button>'
            '<button class="btn primary">Anmelden und herabstufen</button></div></div>')
    with pytest.raises(AssertionError, match="mehr als ein Primärknopf"):
        _regel_pruefen(zwei)


# ── Platzhalter sehen nicht wie eingetragene Werte aus ───────────────────────

_WERTARTIG = re.compile(r"\d+\.\d+\.\d+|@|\b[\w-]+\.(?:de|com|net|at|ch|org)\b|^[0-9a-f]{8}-|://")


def test_beispiel_platzhalter_beginnen_mit_z_b():
    """„203.0.113.10“ als Platzhalter las sich im Bildschirmfoto wie ein eingetragener Wert —
    neben dem Hinweis „leer lassen“. Beispiele tragen deshalb „z. B.“; Muster (xxxx, …) und
    Vorgaben aus Jinja (`{{ … }}`, der echte Ersatzwert) sind ausgenommen."""
    from pathlib import Path
    vorlagen = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"
    falsch = []
    for datei in sorted(vorlagen.glob("*.html")):
        for wert in re.findall(r'placeholder="([^"]*)"', datei.read_text(encoding="utf-8")):
            if "{{" in wert or "…" in wert or "xxxx" in wert:
                continue
            if _WERTARTIG.search(wert) and not wert.startswith("z. B. "):
                falsch.append(f"{datei.name}: {wert}")
    assert not falsch, falsch


# ── Jede Aktion endet mit einer BLEIBENDEN Ergebnisanzeige in ihrer Karte ────
#
# Anlass: Nach „Connector anlegen“ stand das Ergebnis nur kurz am Knopf; danach zeigte die
# Karte nur das Abzeichen „Eingerichtet“. Je Aktion (onclick/onchange in der Einrichtung samt
# Let's-Encrypt-Baustein) steht hier, woran ihr Ergebnis nach dem Neuladen zu sehen ist —
# eine neue Aktion ohne Eintrag lässt den Test scheitern.

BLEIBEND = {
    "passwortSetzen": ["Eigenes Passwort gesetzt"],
    "hostnameSpeichern": ['<span class="step-status done">{{ e.hostname }}</span>'],
    "bootstrapSpeichern": ["✓ {{ e.bootstrap_client_id }}"],
    "anmelden": ['App-Registrierung <strong>{{ s.GATEWAY_NAME }}</strong>', 'id="rechte-ergebnis"'],
    "einfuegen": ['App-Registrierung <strong>{{ s.GATEWAY_NAME }}</strong>'],
    "connectorEinrichten": ['id="conn-ergebnis"'],
    "herabstufen": ['id="rechte-ergebnis"'],
    "rechteMessen": ['id="messung-ergebnis"'],
    "schluesselErneuern": ["{{ e.auth_cert_lauf.text }}"],
    "abfragen": ["(Stand {{ e.exo.letzter_erfolg }})"],
    "lernStarten": ['id="lern-ergebnis"'],
    "geraetHinzufuegen": ["{{ e.geraete }} Gerät"],
    "abschliessen": ['<span class="step-status done">Abgeschlossen</span>'],
    "leHttp": ['id="le-status"'],
    "dns01Auto": ['id="le-status"'],
    "dns01Fertig": ['id="le-status"'],
    "dns01Start": ['id="dns01-offen"', "{{ le.dns01.record_value if le.dns01 else '' }}"],
    "dns01Verwerfen": ['id="dns01-start"'],
    "speichern": ['value="{{ s.LE_EMAIL }}"'],
}
OHNE_BLEIBEND = {
    "exoPruefen": "Prüfung ohne Zustandsänderung — die Antwort steht am Knopf bis zum Neuladen",
    "dns01Testen": "Prüfung der Zugangsdaten ohne Zustandsänderung",
    "leMethodeZeigen": "schaltet nur die Anzeige um (onchange)",
    "dns01ProviderZeigen": "schaltet nur die Anzeige um (onchange)",
    "postJSON": "„Erneut durchgehen“ — das Ergebnis ist die neu geladene Einrichtung selbst",
    "pasteZeigen": "blendet nur das Einfügefeld ein (Notweg)",
}


def _vorlagen_text():
    from pathlib import Path
    v = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"
    return "\n".join((v / n).read_text(encoding="utf-8") for n in ("einrichtung.html", "_letsencrypt.html"))


def test_jede_aktion_hat_eine_bleibende_ergebnisanzeige():
    text = _vorlagen_text()
    aktionen = set(re.findall(r'on(?:click|change)="([A-Za-z0-9_]+)\(', text))
    assert aktionen == set(BLEIBEND) | set(OHNE_BLEIBEND), (
        "ohne Eintrag:", aktionen - set(BLEIBEND) - set(OHNE_BLEIBEND),
        "veraltet:", (set(BLEIBEND) | set(OHNE_BLEIBEND)) - aktionen)
    for aktion, merkmale in BLEIBEND.items():
        for m in merkmale:
            assert m in text, f"{aktion}: Ergebnisanzeige {m!r} fehlt in der Vorlage"


def test_connector_ergebnis_bleibt_mit_zeitpunkt_und_variante(client, monkeypatch):  # noqa: F811
    import exo_setup
    import settings_store
    _anmelden(client)
    settings_store.update({**_GRUND, **_APP})
    import tls_cert
    monkeypatch.setattr(exo_setup, "_voraussetzungen", lambda: (True, ""))
    monkeypatch.setattr(exo_setup, "_run", lambda args: {"ok": True, "output": "ok"})
    monkeypatch.setattr(tls_cert, "info", lambda: {"vorhanden": True, "ca_signiert": True, "lets_encrypt": True,
                                                   "staging": False, "selbstsigniert": False,
                                                   "hostnames": ["relay.firma.de"], "not_after": "2027-02-01",
                                                   "aussteller": "CN=R11"})
    assert (exo_setup.SCRIPTS / "setup_relay_connector.ps1").exists()
    r = exo_setup.connector_einrichten("relay.firma.de", [])
    assert r["ok"], r
    lauf = settings_store.get("EXO_CONNECTOR_LAUF")
    assert lauf["hostname"] == "relay.firma.de" and lauf["ips"] == [] and lauf["zeit"].endswith("Z")
    seite = client.get("/einrichtung").text
    kasten = re.search(r'<div class="alert success" id="conn-ergebnis"[^>]*>(.*?)</div>', seite, re.S).group(1)
    assert "✓ Connector angelegt am " + lauf["zeit"][:10] in kasten
    assert "am Zertifikat für <code>relay.firma.de</code>" in kasten
    settings_store.update({"EXO_CONNECTOR_LAUF": {**lauf, "ips": ["203.0.113.10"]}})
    kasten = re.search(r'id="conn-ergebnis"[^>]*>(.*?)</div>', client.get("/einrichtung").text, re.S).group(1)
    assert "an der IP <code>203.0.113.10</code>" in kasten and "Zertifikat" not in kasten


def test_herabgestuft_und_lernmodus_bleiben_sichtbar(client):  # noqa: F811
    from datetime import datetime, timedelta, timezone
    import settings_store
    _anmelden(client)
    settings_store.update({**_GRUND, **_APP, "EXO_CONNECTOR_CREATED": True,
                           "RECHTE_HERABGESTUFT": "2026-11-27T10:00:00Z"})
    seite = client.get("/einrichtung").text
    assert re.search(r'id="rechte-ergebnis"[^>]*>✓ Rechte herabgestuft am 2026-11-27 10:00 UTC', seite)
    assert 'href="#step-5">Schritt 5' in seite
    assert 'id="lern-ergebnis"' not in seite
    settings_store.update({"SMTP_RELAY_LERN_NETZE": ["10.99.0.0/24"],
                           "SMTP_RELAY_LERN_BIS": (datetime.now(timezone.utc) + timedelta(minutes=14, seconds=30)).isoformat()})
    lern = re.search(r'id="lern-ergebnis"[^>]*>(.*?)</div>', client.get("/einrichtung").text, re.S).group(1)
    assert "läuft noch 15 Minuten" in lern and "10.99.0.0/24" in lern


# ── Anmeldung: Zustimmung angekündigt, nur nötige Scopes ─────────────────────

def test_scopes_der_anmeldung_sind_festgeschrieben():
    """Least Privilege: kein offline_access (Refresh-Token wird nie benutzt). Eine neue
    Berechtigung ist eine bewusste Änderung dieser Liste, keine Nebenwirkung."""
    import pkce
    assert pkce.BOOTSTRAP_SCOPES == [
        "https://graph.microsoft.com/Application.ReadWrite.All",
        "https://graph.microsoft.com/AppRoleAssignment.ReadWrite.All",
        "https://graph.microsoft.com/Directory.ReadWrite.All",
        "https://graph.microsoft.com/RoleManagement.ReadWrite.Directory",
    ]
    _, url = pkce.create_session("http://localhost:1/cb")
    assert "offline_access" not in url
    app_dir = __import__("pathlib").Path(pkce.__file__).parent
    for f in app_dir.rglob("*.py"):
        assert "refresh_token" not in f.read_text(encoding="utf-8"), f"{f}: nutzt doch ein Refresh-Token"


@pytest.mark.parametrize("name", list(ZUSTAENDE))
def test_jeder_anmeldeknopf_kuendigt_die_zustimmung_an(client, monkeypatch, name):  # noqa: F811
    import rechte
    import settings_store
    werte, token, _ = ZUSTAENDE[name]
    _anmelden(client)
    settings_store.update(werte)
    monkeypatch.setattr(rechte, "admin_token", lambda: "tok" if token else None)
    seite = client.get("/einrichtung").text
    teile = re.split(r'<button[^>]*onclick="anmelden\(', seite)
    for i, davor in enumerate(teile[:-1]):
        assert "zustimmung-hinweis" in davor, f"{name}: Anmeldeknopf {i + 1} ohne Hinweis auf die Zustimmung"
        assert "Consent on behalf of your organization" in davor


# ── Rückadresse: die Adresse, die der Browser des Admins erreicht ────────────

_OEFFENTLICH = "https://relay.firma.de:8443/auth/callback"


@pytest.fixture
def login_app(client, monkeypatch):  # noqa: F811
    import config
    import settings_store
    monkeypatch.setattr(config, "WEBUI_PORT", 8443)
    _anmelden(client)
    settings_store.update({**_GRUND, "BOOTSTRAP_CLIENT_ID": "boot-1", "BOOTSTRAP_REDIRECT_URIS": [
        "http://localhost:8443/auth/callback", _OEFFENTLICH]})
    return client


def test_rueckadresse_folgt_der_aufruf_adresse_nicht_dem_zertifikatsnamen(login_app):
    """Bis 0.2.60: Rückadresse = öffentlicher Hostname, sobald er an der Login-App stand —
    auch wenn der Admin das Relay über eine andere Adresse aufrief und den Namen gar nicht
    auflösen kann (DNS-01 braucht nur TXT). Das Fenster endete auf NXDOMAIN."""
    intern = login_app.get("/auth/start", params={"zweck": "herabstufen"},
                           headers={"host": "10.99.0.5:8443"}).json()
    assert intern["redirect_uri"] == "http://localhost:8443/auth/callback" and intern["paste"], intern
    aussen = login_app.get("/auth/start", params={"zweck": "herabstufen"},
                           headers={"host": "relay.firma.de:8443"}).json()
    assert aussen["redirect_uri"] == _OEFFENTLICH and not aussen["paste"], aussen


def test_einfuegen_nimmt_eine_rueckadresse_mit_fremdem_host(login_app, monkeypatch):
    """Notweg: Landet das Fenster auf einem unerreichbaren Namen, trägt die kopierte Adresse
    trotzdem — entscheidend sind code und state, nicht der Host."""
    import pkce
    import rechte
    gesehen = {}

    async def tausch(code, verifier, redirect_uri):
        gesehen["code"] = code
        return {"access_token": "tok", "expires_in": 600}

    async def herab(token=None):
        gesehen["token"] = token
        return {"ok": True, "text": "herabgestuft", "zeit": "2026-11-27T10:00:00Z"}
    monkeypatch.setattr(pkce, "exchange_code", tausch)
    monkeypatch.setattr(rechte, "herabstufen", herab)
    state, _ = pkce.create_session(_OEFFENTLICH, "herabstufen")
    r = login_app.post("/api/setup/auth-paste",
                       json={"url": f"https://irgendwo.invalid:8443/auth/callback?code=c-1&state={state}"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert gesehen == {"code": "c-1", "token": "tok"}


def test_aufruf_adresse_wird_an_der_login_app_nachgetragen(monkeypatch):
    import asyncio
    import settings_store
    import setup_wizard
    stand = {"uris": ["http://localhost:8443/auth/callback"]}
    patches = []

    async def gh(art, url, token, json=None):
        if art == "get":
            return {"value": [{"id": "obj-1", "publicClient": {"redirectUris": list(stand["uris"])}}]}
        patches.append(json["publicClient"]["redirectUris"])
        if any("10.99.0.5" in u for u in json["publicClient"]["redirectUris"]):
            raise RuntimeError("abgelehnt")
        stand["uris"] = json["publicClient"]["redirectUris"]
        return {}
    werte = {"BOOTSTRAP_CLIENT_ID": "boot-1", "PUBLIC_HOSTNAME": "relay.firma.de"}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: werte.get(k, d))
    monkeypatch.setattr(settings_store, "update", lambda d: werte.update(d))
    monkeypatch.setattr(setup_wizard, "_gh", gh)
    monkeypatch.setattr(setup_wizard.config, "WEBUI_PORT", 8443)
    asyncio.run(setup_wizard.patch_bootstrap_redirect_uri("tok", "https://relay.intern.firma.de:8443/auth/callback"))
    assert "https://relay.intern.firma.de:8443/auth/callback" in werte["BOOTSTRAP_REDIRECT_URIS"]
    assert _OEFFENTLICH in werte["BOOTSTRAP_REDIRECT_URIS"]
    # Lehnt Entra die Aufruf-Adresse ab, bleiben die übrigen eingetragen.
    asyncio.run(setup_wizard.patch_bootstrap_redirect_uri("tok", "https://10.99.0.5:8443/auth/callback"))
    assert not any("10.99.0.5" in u for u in werte["BOOTSTRAP_REDIRECT_URIS"])
    assert _OEFFENTLICH in werte["BOOTSTRAP_REDIRECT_URIS"]


def test_jeder_anmeldeknopf_hat_den_notweg_zum_einfuegen(client, monkeypatch):  # noqa: F811
    import rechte
    import settings_store
    for name, (werte, token, _) in ZUSTAENDE.items():
        _anmelden(client)
        settings_store.update(werte)
        monkeypatch.setattr(rechte, "admin_token", lambda t=token: "tok" if t else None)
        seite = client.get("/einrichtung").text
        for m in re.finditer(r"<button[^>]*onclick=\"anmelden\(this, '([a-z-]+)'", seite):
            rest = seite[m.end():m.end() + 900]
            assert f"pasteZeigen('{m.group(1)}')" in rest, f"{name}: Anmeldeknopf für {m.group(1)} ohne Notweg"
            assert f'id="{m.group(1)}"' in seite
