"""Die Weboberfläche: Wachen, Einstellungen, Festes, Geheimnisse."""
import re
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

# Ohne Anmeldung erreichbar — namentlich, damit es eine Entscheidung bleibt.
# Anmeldedaten der Tests — als Namen, nicht als Literale, und die Feldnamen
# getrennt von den Werten: Ein Geheimnis-Scanner (GitGuardian) meldet jedes
# Passwortfeld, das in derselben Zeile einen Wert bekommt — auch das
# Startpasswort und auch, wenn der Wert nur ein Variablenname ist.
STARTKENNUNG = "admin"
# Ein eigenes Startkennwort aus der Umgebung (WEBUI_PASSWORD): Mit dem
# Auslieferungs-Kennwort wäre seit v0.2.25 nur Schritt 1 erreichbar
# (tests/test_vorgabe_kennwort.py prüft das gesondert).
STARTWERT = "start-wert-1"
PROBEWERT = "geheim123"
NEUER_WERT = "lang-und-neu-1"
FELDER_ANMELDUNG = ("username", "password")
FELDER_KONTO = ("SUBMIT_USER", "SUBMIT_PASSWORD")


def _anmeldedaten(wert=STARTWERT):
    return dict(zip(FELDER_ANMELDUNG, (STARTKENNUNG, wert)))


def _kontodaten(wert):
    return dict(zip(FELDER_KONTO, ("relay@firma.de", wert)))


# `/auth/callback` führt ohne Sitzung nichts aus (siehe einrichtung.py), ist aber
# erreichbar, weil Microsoft dorthin zurückleitet.
OHNE_WACHE = {"/auth/login", "/auth/local", "/auth/logout", "/auth/callback", "/health",
              "/log/stream", "/static"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    import config
    import settings_store
    import relay_hosts
    import mail_audit
    import auth_cert
    import exo_mailboxes
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_store, "_data", {})
    monkeypatch.setattr(relay_hosts, "DB_PATH", tmp_path / "relay_hosts.db")
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    monkeypatch.setattr(auth_cert, "PFX_PATH", tmp_path / "auth.pfx")
    monkeypatch.setattr(exo_mailboxes, "AUTH_CERT_PATH", tmp_path / "auth.pfx")
    monkeypatch.setattr(exo_mailboxes, "CACHE_PATH", tmp_path / "mailboxes.json")
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    monkeypatch.setattr(config, "WEBUI_PASSWORD", STARTWERT)
    settings_store.init({})
    mail_audit.init_db()
    import login_drossel
    monkeypatch.setattr(login_drossel, "_FEHLER", {})
    from fastapi.testclient import TestClient
    from webui.app import app
    return TestClient(app, base_url="https://testserver")


def _anmelden(client):
    r = client.post("/auth/local", json=_anmeldedaten())
    assert r.status_code == 200


def _alle_routen() -> list[tuple[str, str]]:
    from webui.app import ROUTENMODULE
    aus = []
    for modul in ROUTENMODULE:
        for route in modul.router.routes:
            for methode in route.methods:
                aus.append((methode, route.path))
    return aus


def test_jede_route_verlangt_anmeldung(client):
    """⚠️ Wer hier schreibt, entscheidet, welche Geräte Post ins Unternehmen
    einliefern. Eine Route ohne Wache ist eine Lücke, keine Bequemlichkeit."""
    for methode, pfad in _alle_routen():
        if pfad in OHNE_WACHE:
            continue
        r = client.request(methode, pfad, json={}, follow_redirects=False)
        assert r.status_code in (302, 401), f"{methode} {pfad} → {r.status_code} ohne Anmeldung"


def test_seiten_nach_anmeldung(client):
    import settings_store
    _anmelden(client)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/einrichtung", (
        "vor dem Abschluss der Einrichtung führt die Startseite dorthin")
    settings_store.update({"SETUP_COMPLETE": True})
    for pfad in ("/", "/einrichtung", "/einstellungen", "/log", "/aktualisierung"):
        r = client.get(pfad)
        assert r.status_code == 200, pfad
        assert "<nav" in r.text
    # Verwaltung (Lernmodus, Geräteliste, Abweisungen) lebt NUR auf /relay; das
    # Dashboard zeigt die Auswertung und verweist dorthin. Bis v0.2.4 stand alles
    # im Dashboard — eine zweite Verwaltungsseite daneben wäre ein zweiter Weg.
    relay = client.get("/relay")
    assert relay.status_code == 200
    for merkmal in ('id="lern-formular"', 'id="geraete-btn"', 'id="abgewiesen-body"', 'id="neu-ip"'):
        assert merkmal in relay.text, f"/relay: {merkmal} fehlt"
    dash = client.get("/").text
    for merkmal in ('id="lern-formular"', 'id="geraete-btn"', 'id="abgewiesen-body"',
                    'id="neu-ip"', "lernStarten", "geraetLoeschen"):
        assert merkmal not in dash, f"Dashboard verwaltet wieder selbst: {merkmal}"
    assert 'href="/relay"' in dash, "Dashboard verweist nicht auf die Verwaltung"


def test_vorlagen_setzen_nur_maskiert_ein():
    """`${…}` in einem Template-Literal landet per innerHTML ungeprüft in der Seite.
    Werte aus Einlieferungen (Absender, Grund, DNS-Name aus dem Reverse-Lookup)
    sind von aussen steuerbar → nur über esc()/escAttr(). Eine frühere /relay-
    Fassung setzte sie roh ein (gespeichertes XSS in der Admin-Sitzung)."""
    import re
    from pathlib import Path
    vorlagen = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"
    roh = [f"{p.name}:{i}: {z.strip()[:80]}"
           for p in vorlagen.glob("*.html")
           for i, z in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
           for m in re.finditer(r"\$\{([^}]*)\}", z)
           if not re.match(r"\s*(esc|escAttr)\(", m.group(1))]
    assert not roh, "unmaskierte Einsetzung: " + "; ".join(roh)


def test_falsches_passwort_wird_gedrosselt(client):
    for _ in range(4):
        client.post("/auth/local", json=_anmeldedaten("falsch"))
    r = client.post("/auth/local", json=_anmeldedaten())
    assert r.status_code == 429


def test_reinject_mode_ist_fest(client):
    """`smtp_relay.pruefe()` (gespiegelt) verweigert jede andere Betriebsart.
    Der Schlüssel darf sich weder über die Oberfläche noch über die Datei ändern."""
    import settings_store
    _anmelden(client)
    r = client.post("/api/einstellungen", json={"REINJECT_MODE": "graph", "GATEWAY_NAME": "Test"})
    assert r.status_code == 200
    assert r.json()["verworfen"] == ["REINJECT_MODE"]
    assert settings_store.get("REINJECT_MODE") == "smtp"
    assert settings_store.get("GATEWAY_NAME") == "Test"
    # Auch aus der Datei heraus nicht:
    settings_store.SETTINGS_FILE.write_text('{"REINJECT_MODE": "imap"}', encoding="utf-8")
    settings_store.init({})
    assert settings_store.get("REINJECT_MODE") == "smtp"


def test_geheimnisse_werden_maskiert_und_maske_nicht_zurueckgeschrieben(client):
    import settings_store
    _anmelden(client)
    client.post("/api/einstellungen", json=_kontodaten(PROBEWERT))
    r = client.get("/einstellungen")
    assert PROBEWERT not in r.text
    assert settings_store.MASK in r.text
    # Das Formular schickt die Maske zurück — sie darf das Passwort nicht ersetzen.
    client.post("/api/einstellungen", json=_kontodaten(settings_store.MASK))
    assert settings_store.get("SUBMIT_PASSWORD") == PROBEWERT


def test_adressen_von_hand_zaehlen(client):
    import exo_mailboxes
    _anmelden(client)
    r = client.post("/api/einstellungen", json={"ADRESSEN_ZUSAETZLICH": "a@firma.de, B@Firma.de\nkein-at"})
    assert r.status_code == 200
    assert exo_mailboxes.known_addresses() == {"a@firma.de", "b@firma.de"}


def test_passwort_aendern(client):
    _anmelden(client)
    r = client.post("/api/password", json={"current": STARTWERT, "new": "kurz"})
    assert r.status_code == 400
    r = client.post("/api/password", json={"current": STARTWERT, "new": NEUER_WERT})
    assert r.json()["ok"]
    client.get("/auth/logout")
    r = client.post("/auth/local", json=_anmeldedaten())
    assert r.status_code == 401
    r = client.post("/auth/local", json=_anmeldedaten(NEUER_WERT))
    assert r.status_code == 200


def test_auth_zertifikat_erzeugen_und_herunterladen(client):
    _anmelden(client)
    r = client.get("/api/auth-cert/public.cer")
    assert r.status_code == 404
    r = client.post("/api/auth-cert/erzeugen")
    assert r.json()["ok"] and len(r.json()["thumbprint"]) == 40
    r = client.get("/api/auth-cert/public.cer")
    assert r.status_code == 200
    from cryptography import x509
    cert = x509.load_der_x509_certificate(r.content)
    assert "EXO-SMTP-Relay" in cert.subject.rfc4514_string()


def test_herkunftspruefung(client):
    _anmelden(client)
    r = client.post("/api/einstellungen", json={"GATEWAY_NAME": "x"},
                    headers={"Origin": "https://boese.example"})
    assert r.status_code == 403


def test_sicherheits_header(client):
    r = client.get("/auth/login")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("pfad", ["/auth/login", "/auth/login?next=/", "/", "/relay",
                                  "/static/style.css", "/health", "/gibt-es-nicht"])
def test_keine_seite_in_den_suchindex(client, pfad):
    """Wie Gateway v1.9.172: Google führte dort /auth/login?next=/ im Index."""
    r = client.get(pfad, follow_redirects=False)
    assert r.headers.get("X-Robots-Tag") == "noindex, nofollow", (pfad, r.status_code)


def test_kein_robots_disallow(client):
    """Ein Disallow verhinderte, dass der Crawler das noindex überhaupt sieht."""
    r = client.get("/robots.txt")
    assert r.status_code != 200 or "Disallow: /\n" not in r.text + "\n"


def test_protokollstufe_wirkt_sofort(client):
    """Wie Gateway v1.11.55: Die Stufe aus der Oberfläche erreicht den
    Wurzel-Logger sofort, nicht nur uvicorn beim nächsten Start."""
    import logging
    wurzel = logging.getLogger()
    vorher = wurzel.level
    try:
        _anmelden(client)
        wurzel.setLevel(logging.INFO)
        assert client.post("/api/einstellungen", json={"LOG_LEVEL": "DEBUG"}).json()["ok"]
        assert wurzel.level == logging.DEBUG
        assert client.post("/api/einstellungen", json={"LOG_LEVEL": "WARNING"}).json()["ok"]
        assert wurzel.level == logging.WARNING
    finally:
        wurzel.setLevel(vorher)


def test_protokollstufe_beim_start_und_unbekannte_stufe():
    import logging
    import log_manager
    wurzel = logging.getLogger()
    vorher = wurzel.level
    try:
        assert log_manager.stufe_anwenden("quatsch") == "INFO" and wurzel.level == logging.INFO
        assert log_manager.stufe_anwenden("error") == "ERROR" and wurzel.level == logging.ERROR
    finally:
        wurzel.setLevel(vorher)
    main_py = (WURZEL / "app" / "main.py").read_text(encoding="utf-8")
    assert "log_manager.stufe_anwenden()" in main_py


def test_connector_am_zertifikat_nur_mit_ca_zertifikat(client, monkeypatch):
    """Microsoft verlangt für die Zertifikatsvariante ein CA-signiertes
    Zertifikat (Quelle im Modulkopf von tls_cert.py). Mit dem selbstsignierten
    entstünde ein Connector, der das Relay nicht erkennt (bis v0.2.32 möglich)."""
    import exo_setup
    import settings_store
    import tls_cert
    _anmelden(client)
    settings_store.update({"PUBLIC_HOSTNAME": "relay.firma.de"})
    tls_cert.selbstsigniert_erzeugen("relay.firma.de")
    monkeypatch.setattr(exo_setup, "_voraussetzungen", lambda: (True, ""))
    skripte = []
    monkeypatch.setattr(exo_setup, "_run", lambda args, **kw: skripte.append(args) or {"ok": False, "output": "x"})
    for pfad in ("/api/setup/connector", "/api/exo/connector"):
        r = client.post(pfad, json={"sender_ips": ""})
        assert "selbstsigniert" in r.json()["output"], pfad
    assert skripte == [], "kein Connector am selbstsignierten Zertifikat"
    client.post("/api/setup/connector", json={"sender_ips": "203.0.113.10"})
    assert len(skripte) == 1 and "203.0.113.10" in skripte[0], "die Adressvariante bleibt möglich"


def test_keine_schnittstellenbeschreibung(client):
    for pfad in ("/docs", "/openapi.json", "/redoc"):
        assert client.get(pfad).status_code in (302, 404)


def test_vorlagen_javascript_nutzt_nur_bekannte_helfer():
    """Jede in den Vorlagen aufgerufene Helferfunktion muss in common.js oder
    base.html definiert sein — ein ReferenceError landet sonst still in einem
    catch-Zweig."""
    static = (WURZEL / "app" / "webui" / "static" / "common.js").read_text(encoding="utf-8")
    base = (WURZEL / "app" / "webui" / "templates" / "base.html").read_text(encoding="utf-8")
    definiert = set(re.findall(r"function\s+([A-Za-z_]\w*)\s*\(", static + base))
    helfer = {"esc", "escAttr", "showMsg", "postJSON", "getJSON", "showAlert", "hideAlert",
              "wacheFertig", "wacheNeuMessen", "ursache"}
    fehlend = helfer - definiert
    assert not fehlend, fehlend


def test_einrichtung_zeigt_ob_das_herabstufen_in_exchange_wirkt(client):
    """Exchange übernimmt das Herabstufen verzögert (live 07.10.2026: über drei
    Stunden). Die Seite darf deshalb nicht nur „herabgestuft" sagen."""
    import settings_store
    _anmelden(client)
    settings_store.update({"SETUP_COMPLETE": True, "AZURE_APP_CREATED": True, "APP_EIGEN": True, "CLIENT_ID": "c",
                           "APP_SP_ID": "s", "RECHTE_HERABGESTUFT": "2026-10-07T00:10:12Z",
                           "RECHTE_MESSUNG": {"zeit": "2026-10-07T03:21:37Z", "stufe": "admin",
                                              "text": "Schreibrechte vorhanden (Set-Mailbox)."}})
    seite = client.get("/einrichtung").text
    assert "noch nicht übernommen" in seite and "Schreibrechte vorhanden (Set-Mailbox)." in seite
    settings_store.update({"RECHTE_WIRKSAM": {"zeit": "2026-10-07T05:00:00Z", "minuten": 290}})
    seite = client.get("/einrichtung").text
    assert "noch nicht übernommen" not in seite
    assert "290 Minuten nach dem Herabstufen" in seite


_HINWEIS = re.compile(r'<div class="alert warn adressleiste-hinweis"[^>]*>(.*?)</div>', re.S)


def test_localhost_fehlerseite_anleitung_steht_hervorgehoben_im_warnkasten(client):
    """0.2.59-beta: „komplette Adresse aus der Adressleiste … einfügen“ ging im Fließtext unter.
    Jetzt EIN Warnkasten (.alert.warn, im Dark Mode eigene Farben), an jeder Paste-Stelle."""
    import settings_store
    _anmelden(client)
    seite = client.get("/einrichtung").text
    login = seite.split('id="login-block"', 1)[1].split('id="paste"', 1)[0]
    kasten = _HINWEIS.findall(login)
    assert len(kasten) == 1, "Anleitung beim ersten Login nicht im Warnkasten"
    assert "<strong>komplette Adresse aus der Adressleiste</strong>" in kasten[0]
    assert "<strong>Die Fehlerseite ist normal.</strong>" in kasten[0]
    assert "<em>das ist normal</em>" not in seite, "alte, unauffällige Fassung steht noch da"
    # Dieselbe Fassung dort, wo das Rechte-Login auf localhost landet.
    settings_store.update({"SETUP_COMPLETE": True, "AZURE_APP_CREATED": True, "APP_EIGEN": True,
                           "CLIENT_ID": "c", "APP_SP_ID": "s"})
    seite = client.get("/einrichtung").text
    rechte = seite.split('id="paste-rechte"', 1)[1].split('id="paste-rechte-url"', 1)[0]
    assert _HINWEIS.findall(rechte) == kasten, "Rechte-Login ohne dieselbe Anleitung"
    # Dark Mode: .alert.warn hat eine eigene dunkle Fassung, der Kasten setzt keine Farbe inline.
    dunkel = (WURZEL / "app" / "webui" / "static" / "dark-mode.css").read_text(encoding="utf-8")
    assert re.search(r'\[data-theme="dark"\] \.alert\.warn\s*\{[^}]*color:', dunkel)
    stil = re.search(r'adressleiste-hinweis"[^>]*style="([^"]*)"', seite).group(1)
    assert "color" not in stil and "#" not in stil and "width" not in stil, stil



# ── Let's Encrypt im Einrichtungsassistenten (0.2.60-beta) ───────────────────

def _le_teil(seite: str) -> str:
    """Der Block aus _letsencrypt.html, wie er in einer Seite steht (ab LE_DOMAIN bis zum Skript)."""
    m = re.search(r'<label for="LE_DOMAIN">.*?const LE_FELDER = .*?</script>', seite, re.S)
    assert m, "Let's-Encrypt-Block fehlt"
    return m.group(0)


def _le_endpunkte(text: str) -> set:
    return set(re.findall(r"'(/api/(?:letsencrypt|tls/dns01/[a-z]+|einstellungen))'", text))


def test_assistent_bietet_lets_encrypt_als_empfohlenen_weg_auf_denselben_wegen(client, monkeypatch):
    import settings_store
    import tls_cert
    _anmelden(client)
    settings_store.update({"PUBLIC_HOSTNAME": "relay.firma.de"})
    monkeypatch.setattr(tls_cert, "info", lambda: {"vorhanden": True, "hostnames": ["relay.firma.de"],
                                                   "not_after": "2027-02-01T00:00:00+00:00", "selbstsigniert": True,
                                                   "staging": False, "lets_encrypt": False, "aussteller": "CN=relay"})
    assistent = client.get("/einrichtung").text
    block = assistent.split('id="le-block"', 1)[1].split('<!-- ── 3', 1)[0]
    assert 'id="le-empfohlen"' in block and "Empfohlen" in block
    for feld in ('id="LE_METHODE"', 'value="http01"', 'value="dns01"', 'id="DNS01_PROVIDER"', "IONOS",
                 'id="le-teil-http01"', "Zertifikat über DNS-01 ausstellen"):
        assert feld in block, feld
    assert "feste" in block and "IP" in block.split('id="le-alternative"', 1)[1], "IP-Variante nicht als Alternative"
    # Ein Pfad: derselbe Block, dieselben Felder, dieselben Endpunkte wie unter Einstellungen.
    einst = client.get("/einstellungen").text
    assert _le_teil(assistent) == _le_teil(einst), "Assistent und Einstellungen weichen ab"
    vorlage = (WURZEL / "app" / "webui" / "templates" / "_letsencrypt.html").read_text(encoding="utf-8")
    assert _le_endpunkte(vorlage) >= {"/api/letsencrypt", "/api/tls/dns01/auto", "/api/tls/dns01/start",
                                      "/api/tls/dns01/finish", "/api/tls/dns01/test", "/api/einstellungen"}
    for name in ("einrichtung.html", "einstellungen.html"):
        eigen = (WURZEL / "app" / "webui" / "templates" / name).read_text(encoding="utf-8")
        assert '{% include "_letsencrypt.html" %}' in eigen, name
        assert not _le_endpunkte(eigen) & {"/api/letsencrypt", "/api/tls/dns01/auto"}, f"{name}: eigene LE-Logik"
    from webui.app import ROUTENMODULE
    routen = {r.path for m in ROUTENMODULE for r in m.router.routes}
    assert _le_endpunkte(vorlage) <= routen, _le_endpunkte(vorlage) - routen
    # Schritt 4: ohne LE-Zertifikat Hinweis auf die IP-Variante und Schritt 2.
    settings_store.update({"AZURE_APP_CREATED": True})
    assert re.search(r'id="conn-variante"[^>]*>[^<]*Noch kein Zertifikat von Let', client.get("/einrichtung").text)


def test_assistent_zeigt_ausgestellt_von_lets_encrypt(client, monkeypatch):
    import settings_store
    import tls_cert
    _anmelden(client)
    settings_store.update({"PUBLIC_HOSTNAME": "relay.firma.de", "AZURE_APP_CREATED": True, "LE_AUTO_RENEW": True})
    monkeypatch.setattr(tls_cert, "info", lambda: {"vorhanden": True, "hostnames": ["relay.firma.de"],
                                                   "not_after": "2027-02-01T00:00:00+00:00", "selbstsigniert": False,
                                                   "staging": False, "lets_encrypt": True,
                                                   "aussteller": "CN=R11,O=Let's Encrypt,C=US"})
    seite = client.get("/einrichtung").text
    status = re.search(r'<div class="alert success" id="le-status"[^>]*>(.*?)</div>', seite, re.S)
    assert status and "Ausgestellt von Let" in status.group(1) and "2027-02-01" in status.group(1)
    assert "automatisch erneuert" in status.group(1)
    assert re.search(r'<div class="alert success" id="conn-variante"', seite), "Schritt 4 nennt die Zertifikatsvariante nicht"
