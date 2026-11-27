"""DNS-01 über eine DNS-API (0.2.57-beta): Provider-Katalog, IONOS, Ablauf.

Ersetzt sind nur die Fremdsysteme: die IONOS-API (httpx.MockTransport, nach
der OpenAPI-Beschreibung „DNS API“ 1.0.2), der autoritative Nameserver (ein
echter UDP-Server auf 127.0.0.1, der aus dem Zustand der Fake-IONOS antwortet)
und Let's Encrypt (prüft beim Auslösen der Challenge, ob der TXT-Wert im DNS
steht — wie der echte Validator). Der DNS-Drahtformat-Code, das Warten, das
Aufräumen, die Übernahme ans Listener-Zertifikat und die Oberfläche laufen echt.
"""
import ast
import asyncio
import json
import logging
import socket
import struct
import sys
import threading
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa

from test_letsencrypt import _blatt, _ca, umgebung  # noqa: F401  (Fixture)
from test_oberflaeche import _anmelden, client  # noqa: F401  (Fixture)

WURZEL = Path(__file__).resolve().parent.parent
KATALOG = WURZEL / "app" / "dns_provider"

PRAEFIX = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
GEHEIM = "GeHeIm-Sekret-xyz-0123456789abcdefGHIJ"
SPF = "v=spf1 include:spf.protection.outlook.com -all"
FREMD_ACME = "fremder-wert-eines-anderen-acme-clients"


# ── Fake IONOS (nach dns.yaml: Zonen, Records, X-API-Key, 401) ──────────────

class FakeIonos:
    def __init__(self, echo_key_bei_401=False):
        self.zonen = {"example.com": "z-1", "example.org": "z-2"}
        self.records = [  # vorhandene, FREMDE Records
            {"id": "r-spf", "zone": "z-1", "name": "example.com", "type": "TXT", "content": SPF},
            {"id": "r-fremd", "zone": "z-1", "name": "_acme-challenge.relay.example.com",
             "type": "TXT", "content": FREMD_ACME},
            {"id": "r-a", "zone": "z-1", "name": "relay.example.com", "type": "A", "content": "192.0.2.10"},
        ]
        self.anfragen: list[tuple[str, str]] = []
        self.echo = echo_key_bei_401
        self._n = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.anfragen.append((req.method, req.url.path))
        if req.headers.get("X-API-Key") != f"{PRAEFIX}.{GEHEIM}":
            body = [{"code": "UNAUTHORIZED",
                     "message": "The customer is not authorized to do this operation."
                     + (f" key={req.headers.get('X-API-Key')}" if self.echo else "")}]
            return httpx.Response(401, json=body)
        teile = req.url.path.split("/")          # /dns/v1/zones[/id[/records[/rid]]]
        assert teile[:4] == ["", "dns", "v1", "zones"], req.url
        if len(teile) == 4 and req.method == "GET":
            return httpx.Response(200, json=[{"id": i, "name": n, "type": "NATIVE"}
                                             for n, i in self.zonen.items()])
        zid = teile[4]
        if len(teile) == 5 and req.method == "GET":
            name = req.url.params.get("recordName")
            typ = req.url.params.get("recordType")
            recs = [self._aus(r) for r in self.records if r["zone"] == zid
                    and (not name or r["name"] == name) and (not typ or r["type"] in typ.split(","))]
            return httpx.Response(200, json={"id": zid, "name": "?", "type": "NATIVE", "records": recs})
        if len(teile) == 6 and req.method == "POST":
            neu = json.loads(req.content)
            assert isinstance(neu, list), "IONOS erwartet eine LISTE von Records"
            aus = []
            for rec in neu:
                self._n += 1
                r = {"id": f"r-neu-{self._n}", "zone": zid, "name": rec["name"],
                     "type": rec["type"], "content": rec["content"]}
                self.records.append(r)
                aus.append(self._aus(r))
            return httpx.Response(201, json=aus)
        if len(teile) == 7 and req.method == "DELETE":
            vorher = len(self.records)
            self.records = [r for r in self.records if not (r["zone"] == zid and r["id"] == teile[6])]
            return httpx.Response(200 if len(self.records) < vorher else 404)
        return httpx.Response(405)

    @staticmethod
    def _aus(r):
        inhalt = f'"{r["content"]}"' if r["type"] == "TXT" else r["content"]   # IONOS liefert TXT in "…"
        return {"id": r["id"], "name": r["name"], "rootName": "x", "type": r["type"],
                "content": inhalt, "ttl": 60, "prio": 0, "disabled": False}

    def txt(self, name):
        return [r["content"] for r in self.records if r["type"] == "TXT" and r["name"] == name]


# ── Autoritativer Nameserver auf 127.0.0.1 (echtes Drahtformat) ─────────────

def _name(n: str) -> bytes:
    return b"".join(bytes([len(t)]) + t.encode() for t in n.split(".")) + b"\x00"


class FakeDns:
    """Antwortet autoritativ (AA) mit den TXT-Werten aus `quelle(name)`;
    Antwortnamen als Kompressionszeiger auf die Frage (wie echte Server)."""

    def __init__(self, quelle):
        self.quelle = quelle
        self.fragen = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self.faden = threading.Thread(target=self._lauf, daemon=True)
        self.faden.start()

    def _lauf(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                daten, adr = self.sock.recvfrom(512)
            except (socket.timeout, OSError):
                continue
            self.fragen += 1
            kennung = daten[:2]
            pos, teile = 12, []
            while daten[pos]:
                teile.append(daten[pos + 1:pos + 1 + daten[pos]].decode())
                pos += 1 + daten[pos]
            frage = daten[12:pos + 5]
            name = ".".join(teile).lower()
            werte = list(self.quelle(name))
            kopf = kennung + struct.pack(">HHHHH", 0x8400, 1, len(werte), 0, 0)  # QR|AA
            rr = b""
            for w in werte:
                b = w.encode()
                rdata = b"".join(bytes([len(b[i:i + 255])]) + b[i:i + 255] for i in range(0, len(b), 255))
                rr += b"\xc0\x0c" + struct.pack(">HHIH", 16, 1, 60, len(rdata)) + rdata
            self.sock.sendto(kopf + frage + rr, adr)

    def stop(self):
        self._stop = True
        self.faden.join(2)
        self.sock.close()


# ── Fake Let's Encrypt: prüft beim Auslösen, ob der Wert im DNS steht ───────

class FakeAcme:
    ca = None
    dns = None                       # FakeIonos — was der Validator „sieht“
    aufrufe: list = []
    ausgeloest: list = []
    fehler_beim_ausloesen = None
    letzter_csr = None

    def __init__(self, directory_url, account_key, account_url=""):
        self.account_key, self.account_url = account_key, account_url
        self._status = "pending"
        self._csr = None
        self._authz: dict = {}

    async def ensure_account(self, email=""):
        self.account_url = "https://acme.test/acct/1"
        return self.account_url

    async def _fetch_directory(self):
        return {"newOrder": "https://acme.test/new-order"}

    async def _post(self, url, payload):
        idents = [i["value"] for i in payload["identifiers"]]
        return _Resp(201, self._order(idents), {"Location": "https://acme.test/order/1"})

    async def new_order_dns(self, domain):
        o = self._order([domain])
        o["order_url"] = "https://acme.test/order/1"
        return o

    def _order(self, idents):
        authz = []
        for i, ident in enumerate(idents):
            url = f"https://acme.test/authz/{i}"
            wild = ident.startswith("*.")
            self._authz[url] = {"identifier": {"type": "dns", "value": ident[2:] if wild else ident},
                                "wildcard": wild, "status": "pending",
                                "challenges": [{"type": "http-01", "url": "x", "token": f"h{i}"},
                                               {"type": "dns-01", "url": f"https://acme.test/ch/{i}",
                                                "token": f"tok{i}"}]}
            authz.append(url)
        self.idents = idents
        return {"authorizations": authz, "finalize": "https://acme.test/fin/1"}

    async def get_authorization(self, url):
        return self._authz[url]

    async def trigger_challenge(self, url):
        import acme_client
        import hashlib
        FakeAcme.aufrufe.append(("trigger", url))
        if FakeAcme.fehler_beim_ausloesen:
            raise FakeAcme.fehler_beim_ausloesen
        a = next(a for a in self._authz.values() if a["challenges"][1]["url"] == url)
        tok = a["challenges"][1]["token"]
        soll = acme_client.b64url(hashlib.sha256(
            f"{tok}.{acme_client.jwk_thumbprint(self.account_key)}".encode()).digest())
        name = f"_acme-challenge.{a['identifier']['value']}"
        gesehen = FakeAcme.dns.txt(name)
        FakeAcme.ausgeloest.append((name, soll, list(gesehen)))
        a["status"] = "valid" if soll in gesehen else "invalid"
        if a["status"] == "invalid":
            a["challenges"][1]["error"] = {"detail": f"No TXT record found at {name}"}
        return {"status": "pending"}

    async def poll_order_status(self, url, timeout_sec=600):
        if self._csr is not None:
            return {"status": "valid", "certificate": "https://acme.test/cert/1",
                    "authorizations": list(self._authz)}
        st = [a["status"] for a in self._authz.values()]
        return {"status": "invalid" if "invalid" in st else "ready", "authorizations": list(self._authz)}

    async def finalize(self, url, csr_der):
        self._csr = FakeAcme.letzter_csr = x509.load_der_x509_csr(csr_der)
        return {"status": "processing"}

    async def download_certificate(self, url):
        san = self._csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return _blatt(FakeAcme.ca, san.get_values_for_type(x509.DNSName)[0], 90,
                      public_key=self._csr.public_key())[0]


class _Resp:
    def __init__(self, code, body, headers):
        self.status_code, self._b, self.headers, self.text = code, body, headers, json.dumps(body)

    def json(self):
        return self._b


@pytest.fixture
def dns01(umgebung, monkeypatch):  # noqa: F811
    """Relay mit Fake-IONOS, Fake-Nameserver und Fake-Let's-Encrypt."""
    import acme_client
    import settings_store
    from dns_provider import basis, dnsabfrage
    ionos = FakeIonos()
    monkeypatch.setattr(basis.DnsProvider, "transport", httpx.MockTransport(ionos))
    server = FakeDns(lambda name: ionos.txt(name))
    monkeypatch.setattr(dnsabfrage, "PORT", server.port)
    monkeypatch.setattr(dnsabfrage, "WURZELN", ("127.0.0.1",))
    monkeypatch.setattr(dnsabfrage, "ZEITLIMIT_ABFRAGE", 0.5)
    monkeypatch.setattr(acme_client, "AcmeClient", FakeAcme)
    FakeAcme.ca, FakeAcme.dns, FakeAcme.aufrufe, FakeAcme.ausgeloest = _ca(), ionos, [], []
    FakeAcme.fehler_beim_ausloesen = None
    settings_store.update({"LE_METHODE": "dns01", "DNS01_PROVIDER": "ionos",
                           "DNS01_IONOS_PREFIX": PRAEFIX, "DNS01_IONOS_SECRET": GEHEIM,
                           "LE_DOMAIN": "relay.example.com", "LE_EMAIL": "it@example.com"})
    yield ionos, server
    server.stop()


def _auto(namen, **kw):
    import tls_acme_dns
    kw.setdefault("intervall", 0.05)
    kw.setdefault("zeitlimit", 5)
    return asyncio.run(tls_acme_dns.automatisch(namen, "it@example.com", **kw))


# ── Ablauf ───────────────────────────────────────────────────────────────────

def test_erfolg_txt_sichtbar_validiert_aufgeraeumt_zertifikat_benutzt(dns01):
    import tls_cert
    ionos, server = dns01
    info = _auto(["relay.example.com"])
    name, soll, gesehen = FakeAcme.ausgeloest[0]
    assert name == "_acme-challenge.relay.example.com" and soll in gesehen, \
        "Let's Encrypt sah den TXT-Wert beim Auslösen"
    assert server.fragen >= 2, "der Wert wurde vorher autoritativ abgefragt"
    assert ionos.txt(name) == [FREMD_ACME], "eigener Wert entfernt, fremder steht"
    assert ionos.txt("example.com") == [SPF]
    assert info["hostnames"] == ["relay.example.com"]
    st = tls_cert.info()
    assert st["lets_encrypt"] and st["hostnames"] == ["relay.example.com"]
    key = Path(__import__("config").SMTP_TLS_KEY).read_bytes()
    from cryptography.hazmat.primitives import serialization
    assert isinstance(serialization.load_pem_private_key(key, None), rsa.RSAPrivateKey), "RSA für Altgeräte"


def test_wildcard_und_apex_zwei_werte_unter_einem_namen(dns01):
    ionos, _ = dns01
    _auto(["example.com", "*.example.com"])
    werte = {(n, s) for n, s, _g in FakeAcme.ausgeloest}
    assert {n for n, _ in werte} == {"_acme-challenge.example.com"} and len(werte) == 2
    assert all(s in g for _n, s, g in FakeAcme.ausgeloest), "beide Werte gleichzeitig sichtbar"
    assert ionos.txt("_acme-challenge.example.com") == []
    san = FakeAcme.letzter_csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["example.com", "*.example.com"]


def test_zone_nicht_gefunden(dns01):
    ionos, _ = dns01
    with pytest.raises(ValueError, match="keine Zone für _acme-challenge.relay.example.net"):
        _auto(["relay.example.net"])
    assert not [a for a in ionos.anfragen if a[0] == "POST"]
    assert FakeAcme.aufrufe == [], "ohne TXT keine Challenge"


def test_401_klare_meldung_ohne_geheimnis(dns01, monkeypatch):
    import settings_store
    ionos, _ = dns01
    ionos.echo = True                                    # IONOS wiederholt den Schlüssel
    settings_store.update({"DNS01_IONOS_SECRET": GEHEIM + "-falsch"})
    with pytest.raises(ValueError) as e:
        _auto(["relay.example.com"])
    assert "Zugang abgelehnt (HTTP 401)" in str(e.value)
    assert GEHEIM not in str(e.value)
    assert FakeAcme.aufrufe == []


def test_zeitueberschreitung_der_propagation_raeumt_auf(dns01):
    from dns_provider import dnsabfrage
    ionos, server = dns01
    server.quelle = lambda name: [FREMD_ACME] if name.startswith("_acme") else []   # unser Wert kommt nie an
    with pytest.raises(ValueError, match="nicht auf allen autoritativen Nameservern"):
        _auto(["relay.example.com"], zeitlimit=0.4)
    assert ionos.txt("_acme-challenge.relay.example.com") == [FREMD_ACME], "aufgeräumt"
    assert FakeAcme.aufrufe == [], "Challenge nicht ausgelöst — sonst zählt ein Fehlversuch bei LE"
    assert issubclass(dnsabfrage.PropagationZeitueberschreitung, dnsabfrage.DnsAbfrageFehler)


def test_aufraeumen_auch_wenn_acme_scheitert(dns01):
    ionos, _ = dns01
    FakeAcme.fehler_beim_ausloesen = RuntimeError("ACME challenge trigger failed: 500")
    with pytest.raises(RuntimeError):
        _auto(["relay.example.com"])
    assert ionos.txt("_acme-challenge.relay.example.com") == [FREMD_ACME]


def test_validierung_scheitert_meldung_und_aufraeumen(dns01, monkeypatch):
    """LE sieht den Wert nicht (z. B. falscher Nameserver) → klare Meldung, TXT weg."""
    ionos, _ = dns01
    echt = FakeAcme.trigger_challenge

    async def blind(self, url):
        FakeAcme.dns, gemerkt = FakeIonos(), FakeAcme.dns
        try:
            return await echt(self, url)
        finally:
            FakeAcme.dns = gemerkt
    monkeypatch.setattr(FakeAcme, "trigger_challenge", blind)
    with pytest.raises(ValueError, match="No TXT record found"):
        _auto(["relay.example.com"])
    assert ionos.txt("_acme-challenge.relay.example.com") == [FREMD_ACME]


def test_provider_entfernt_nur_den_eigenen_wert(dns01):
    import tls_acme_dns
    ionos, _ = dns01
    p = tls_acme_dns.provider_aus_einstellungen()
    n = "_acme-challenge.relay.example.com"
    p.txt_anlegen(n, "eigen-1")
    p.txt_anlegen(n, "eigen-1")                          # doppelt: kein zweiter Record
    assert sorted(ionos.txt(n)) == sorted([FREMD_ACME, "eigen-1"])
    p.txt_entfernen(n, "eigen-1")
    p.txt_entfernen(n, "eigen-1")                        # schon weg: kein Fehler
    assert ionos.txt(n) == [FREMD_ACME]
    assert ionos.txt("example.com") == [SPF]


def test_tiefste_zone_gewinnt():
    from dns_provider import zone_waehlen
    assert zone_waehlen("_acme-challenge.a.sub.example.com", ["example.com", "sub.example.com"]) == "sub.example.com"
    assert zone_waehlen("_acme-challenge.example.com", ["example.com"]) == "example.com"
    assert zone_waehlen("x.example.net", ["example.com"]) is None
    assert zone_waehlen("x.badexample.com", ["example.com"]) is None


def test_dns_nicht_erreichbar_wird_gemeldet(monkeypatch):
    from dns_provider import dnsabfrage
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:   # Port, auf dem niemand antwortet
        s.bind(("127.0.0.1", 0))
        monkeypatch.setattr(dnsabfrage, "PORT", s.getsockname()[1])
    monkeypatch.setattr(dnsabfrage, "WURZELN", ("127.0.0.1",))
    monkeypatch.setattr(dnsabfrage, "ZEITLIMIT_ABFRAGE", 0.1)
    with pytest.raises(dnsabfrage.DnsNichtErreichbar, match="Port 53"):
        dnsabfrage.warten("_acme-challenge.example.com", "x", 0.2, 0.05)


def test_verweis_von_der_wurzel_zum_autoritativen(monkeypatch):
    """Wurzel antwortet mit Verweis (NS + Glue), erst der Zielserver autoritativ."""
    from dns_provider import dnsabfrage
    try:
        ziel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ziel.bind(("127.0.0.2", 0))
        ziel.close()
    except OSError:
        pytest.skip("127.0.0.2 nicht bindbar")
    auth = FakeDns(lambda n: ["wert-x"])
    auth.sock.close()
    auth.stop()
    # Zielserver auf 127.0.0.2 mit DEMSELBEN Port wie die Wurzel (PORT ist global)
    wurzel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    wurzel.bind(("127.0.0.1", 0))
    port = wurzel.getsockname()[1]
    wurzel.close()
    s_ziel = FakeDns.__new__(FakeDns)
    s_ziel.quelle, s_ziel.fragen, s_ziel._stop = (lambda n: ["wert-x"]), 0, False
    s_ziel.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s_ziel.sock.bind(("127.0.0.2", port))
    s_ziel.faden = threading.Thread(target=s_ziel._lauf, daemon=True)
    s_ziel.faden.start()

    sw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sw.bind(("127.0.0.1", port))
    sw.settimeout(0.2)
    halt = threading.Event()

    def verweis():
        while not halt.is_set():
            try:
                d, adr = sw.recvfrom(512)
            except (socket.timeout, OSError):
                continue
            pos = 12
            while d[pos]:
                pos += 1 + d[pos]
            frage = d[12:pos + 5]
            ns = _name("ns1.example.net")
            kopf = d[:2] + struct.pack(">HHHHH", 0x8000, 1, 0, 1, 1)       # kein AA
            auth_rr = _name("example.com") + struct.pack(">HHIH", 2, 1, 60, len(ns)) + ns
            glue = ns + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton("127.0.0.2")
            sw.sendto(kopf + frage + auth_rr + glue, adr)
    t = threading.Thread(target=verweis, daemon=True)
    t.start()
    try:
        monkeypatch.setattr(dnsabfrage, "PORT", port)
        monkeypatch.setattr(dnsabfrage, "WURZELN", ("127.0.0.1",))
        assert dnsabfrage.warten("_acme-challenge.example.com", "wert-x", 2, 0.05) == ["ns1.example.net"]
    finally:
        halt.set()
        t.join(2)
        sw.close()
        s_ziel.stop()


# ── Erneuerung respektiert den Methodenschalter ──────────────────────────────

def test_erneuerung_ueber_dns01_ohne_certbot(dns01, monkeypatch):
    import le_certbot
    import scheduler
    import settings_store
    import tls_cert
    pem, key = _blatt(FakeAcme.ca, "relay.example.com", 5)
    tls_cert._schreiben(pem, key)
    monkeypatch.setattr(le_certbot, "erneuern", lambda *a, **k: pytest.fail("certbot bei DNS-01 gefragt"))
    monkeypatch.setattr(scheduler, "_le_versuch_tag", "")
    import tls_acme_dns
    echt = tls_acme_dns.automatisch
    monkeypatch.setattr(tls_acme_dns, "automatisch",
                        lambda *a, **k: echt(*a, **{**k, "zeitlimit": 5, "intervall": 0.05}))
    assert scheduler._tls_pruefen() == "renewed"
    assert tls_cert.info()["tage_rest"] > 80
    lauf = settings_store.get("LE_LAUF")
    assert lauf["ok"] and lauf["weg"] == "dns01" and "ionos" in lauf["text"]


def test_erneuerung_dns01_ohne_provider_bleibt_manuell(dns01, monkeypatch):
    import le_certbot
    import scheduler
    import settings_store
    import tls_cert
    pem, key = _blatt(FakeAcme.ca, "relay.example.com", 5)
    tls_cert._schreiben(pem, key)
    settings_store.update({"DNS01_PROVIDER": ""})
    monkeypatch.setattr(le_certbot, "erneuern", lambda *a, **k: pytest.fail("certbot bei DNS-01 gefragt"))
    monkeypatch.setattr(scheduler, "_le_versuch_tag", "")
    assert scheduler._tls_pruefen() == "manuell"


def test_erneuerung_ueberschreibt_kein_fremdes_zertifikat(dns01, monkeypatch):
    import scheduler
    import tls_cert
    pem, key = _blatt(_ca(org="Firmen-CA", cn="Intern"), "relay.example.com", 5)
    tls_cert._schreiben(pem, key)
    monkeypatch.setattr(scheduler, "_le_versuch_tag", "")
    assert scheduler._tls_pruefen() == "manuell"
    assert FakeAcme.aufrufe == []


def test_erneuerung_http01_bleibt_bei_certbot(dns01, monkeypatch):
    import le_certbot
    import scheduler
    import settings_store
    import tls_cert
    pem, key = _blatt(FakeAcme.ca, "relay.example.com", 5)
    tls_cert._schreiben(pem, key)
    settings_store.update({"LE_METHODE": "http01"})
    rufe = []
    monkeypatch.setattr(le_certbot, "erneuern", lambda *a, **k: rufe.append(1) or ("error", "x"))
    monkeypatch.setattr(scheduler, "_le_versuch_tag", "")
    scheduler._tls_pruefen()
    assert rufe == [1] and FakeAcme.aufrufe == []


# ── Geheimnisse ──────────────────────────────────────────────────────────────

def test_keine_geheimnisse_in_log_status_und_antworten(client, dns01, caplog):  # noqa: F811
    import settings_store
    ionos, server = dns01
    caplog.set_level(logging.DEBUG)
    _anmelden(client)
    # Erfolg über die Oberfläche
    r = client.post("/api/tls/dns01/auto", json={"domain": "relay.example.com", "email": "it@example.com"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    antworten = [r.text]
    # Verbindung testen: maskiertes Geheimnis steht für das gespeicherte, nur GET
    vorher = len(ionos.anfragen)
    r = client.post("/api/tls/dns01/test", json={"provider": "ionos", "werte": {
        "DNS01_IONOS_PREFIX": PRAEFIX, "DNS01_IONOS_SECRET": settings_store.MASK}})
    assert r.json() == {"ok": True, "provider": "ionos", "zonen": ["example.com", "example.org"]}
    assert {m for m, _ in ionos.anfragen[vorher:]} == {"GET"}, "Verbindung testen schreibt nichts"
    antworten.append(r.text)
    # 401 mit Echo des Schlüssels
    ionos.echo = True
    r = client.post("/api/tls/dns01/test", json={"provider": "ionos", "werte": {
        "DNS01_IONOS_PREFIX": PRAEFIX, "DNS01_IONOS_SECRET": GEHEIM + "-neu"}})
    assert r.status_code == 502 and "401" in r.json()["error"]
    antworten.append(r.text)
    settings_store.update({"DNS01_IONOS_SECRET": GEHEIM + "-neu"})
    r = client.post("/api/tls/dns01/auto", json={"domain": "relay.example.com", "email": "it@example.com"})
    assert r.status_code == 502
    antworten.append(r.text)
    settings_store.update({"DNS01_IONOS_SECRET": GEHEIM})
    # Seite, Status
    seite = client.get("/einstellungen").text
    antworten += [seite, json.dumps(settings_store.get("LE_LAUF")), json.dumps(settings_store.public_view())]
    for geheim in (GEHEIM, GEHEIM + "-neu"):
        for i, text in enumerate(antworten):
            assert geheim not in text, f"Geheimnis in Antwort {i}"
        assert geheim not in caplog.text, "Geheimnis im Protokoll"
    assert f'id="DNS01_IONOS_SECRET" value="{settings_store.MASK}"' in seite, "nur maskiert"
    assert 'type="password" id="DNS01_IONOS_SECRET"' in seite
    # … aber gespeichert, und zwar in settings.json unter data/ (secure_io, 600/ACL)
    import config
    gespeichert = json.loads((Path(config.DATA_DIR) / "settings.json").read_text())
    assert gespeichert["DNS01_IONOS_SECRET"] == GEHEIM


def test_maskiertes_geheimnis_ueberschreibt_nicht(client, dns01):  # noqa: F811
    import settings_store
    _anmelden(client)
    settings_store.update({"DNS01_IONOS_SECRET": GEHEIM})
    r = client.post("/api/einstellungen", json={"DNS01_IONOS_SECRET": settings_store.MASK,
                                                "DNS01_PROVIDER": "ionos"})
    assert r.json()["ok"]
    assert settings_store.get("DNS01_IONOS_SECRET") == GEHEIM
    r = client.post("/api/einstellungen", json={"DNS01_PROVIDER": "gibtsnicht"})
    assert r.status_code == 400
    r = client.post("/api/einstellungen", json={"LE_METHODE": "tls-alpn"})
    assert r.status_code == 400


def test_geheimnisse_des_katalogs_sind_secret_keys():
    import dns_provider
    import settings_store
    import tls_acme_dns
    for name, cls in dns_provider.PROVIDER.items():
        for f in cls.FELDER:
            k = tls_acme_dns.feld_schluessel(name, f.schluessel)
            assert k in settings_store.DEFAULTS, k
            assert (k in settings_store.SECRET_KEYS) == f.geheim, k


# ── Knopf mit Gegenstück: Oberfläche ↔ Einstellung ↔ Doku ────────────────────

def test_jede_dns01_einstellung_hat_oberflaeche_und_wird_gespeichert(client):  # noqa: F811
    import re
    import settings_store
    _anmelden(client)
    settings_store.update({"LE_METHODE": "dns01", "DNS01_PROVIDER": "ionos"})
    seite = client.get("/einstellungen").text
    m = re.search(r"const LE_FELDER = (\[.*?\]);", seite)
    gespeichert = set(json.loads(m.group(1)))
    schluessel = [k for k in settings_store.DEFAULTS if k.startswith("DNS01_")] + ["LE_METHODE"]
    assert len(schluessel) >= 11
    for k in schluessel:
        assert f'id="{k}"' in seite, f"{k} ohne Oberflächenpunkt"
        assert k in gespeichert, f"{k} wird nicht gespeichert"
    assert 'id="dns01-test-btn"' in seite and "Verbindung testen" in seite
    readme = (WURZEL / "README.md").read_text("utf-8")
    for k in ("LE_METHODE", "DNS01_PROVIDER", "DNS01_IONOS_PREFIX", "DNS01_IONOS_SECRET", "DNS01_WARTEN_S"):
        assert k in readme, f"{k} nicht im README"


def test_http01_und_manuell_lehnen_wildcard_ab(client):  # noqa: F811
    _anmelden(client)
    r = client.post("/api/letsencrypt", json={"domain": "*.example.com", "email": "it@example.com"})
    assert r.status_code == 400 and "DNS-01" in r.json()["error"]
    r = client.post("/api/tls/dns01/start", json={"domain": "*.example.com", "email": "it@example.com"})
    assert r.status_code == 400 and "Provider" in r.json()["error"]


def test_vorgabe_der_methode_je_plattform():
    """Windows hat kein certbot — dort ist DNS-01 die Vorgabe."""
    import settings_store
    assert settings_store.DEFAULTS["LE_METHODE"] == ("dns01" if sys.platform == "win32" else "http01")


def test_namen_aus():
    import tls_acme_dns
    assert tls_acme_dns.namen_aus("Relay.Example.com, *.example.com relay.example.com") == \
        ["relay.example.com", "*.example.com"]
    for falsch in ("a.*.example.com", "*", "foo", "-x.example.com", "x_y.example.com"):
        with pytest.raises(ValueError):
            tls_acme_dns.namen_aus(falsch)


# ── Katalog: eigenständig, Manifest, Lizenz ──────────────────────────────────

def test_manifest_passt_zum_inhalt():
    import dns_provider
    fassung, soll = dns_provider.manifest_lesen()
    assert fassung and fassung == dns_provider.__version__
    assert soll == dns_provider.manifest_berechnen(), (
        "KATALOG_VERSION veraltet — cd app && python -m dns_provider schreiben <neue Fassung>")


# Gleichlauf Relay ↔ Gateway: Ablauf in app/dns_provider/LIESMICH.md. Das
# Gateway trägt dieselben Prüfungen (Gegenstück), damit ein Inhalt nie unter
# einer schon vergebenen Fassung auftaucht.

def _fingerabdruck(summen: dict[str, str]) -> str:
    import hashlib
    return hashlib.sha256("".join(f"{s}  {d}\n" for d, s in sorted(summen.items())).encode()).hexdigest()


def _fassung_tupel(fassung: str) -> tuple[int, int, int]:
    teile = fassung.split(".")
    assert len(teile) == 3 and all(t.isdigit() for t in teile), f"Fassung {fassung!r} ist kein X.Y.Z"
    return tuple(int(t) for t in teile)


def _register() -> list[tuple[str, str]]:
    zeilen = [z.split() for z in (KATALOG / "FASSUNGEN").read_text("utf-8").splitlines()
              if z.strip() and not z.lstrip().startswith("#")]
    assert all(len(z) == 2 and len(z[1]) == 64 for z in zeilen), "FASSUNGEN: Zeile ist nicht »<fassung>  <sha256>«"
    return [(z[0], z[1]) for z in zeilen]


def test_katalog_fassung_im_register():
    """Manifest geändert ohne Versionssprung → rot. Die laufende Fassung muss die
    letzte im Register sein und genau diesen Inhalt tragen; Fassungen steigen."""
    import dns_provider
    fassung, soll = dns_provider.manifest_lesen()
    register = _register()
    fassungen = [f for f, _ in register]
    assert len(set(fassungen)) == len(fassungen), "FASSUNGEN: eine Fassung doppelt"
    tupel = [_fassung_tupel(f) for f in fassungen]
    assert tupel == sorted(tupel), "FASSUNGEN: Fassungen nicht aufsteigend"
    fp = _fingerabdruck(soll)
    eintrag = dict(register).get(fassung)
    assert eintrag is not None and fassungen[-1] == fassung, (
        f"Fassung {fassung} fehlt am Ende von app/dns_provider/FASSUNGEN — "
        f"neue Fassung? Anhängen: »{fassung}  {fp}« (Ablauf: app/dns_provider/LIESMICH.md)")
    assert eintrag == fp, (
        f"Inhalt geändert, Fassung {fassung} aber nicht angehoben — "
        f"cd app && python -m dns_provider schreiben <neue Fassung>, dann Zeile in FASSUNGEN anhängen")


def test_katalog_fassungen_wie_in_der_historie():
    """Jede je committete Fassung (KATALOG_VERSION in der Git-Historie) steht mit
    DEMSELBEN Fingerabdruck im Register — gegen das Umschreiben einer Registerzeile.
    Ohne Git (Paket) übersprungen; im flachen CI-Checkout nur die vorhandenen Commits."""
    import subprocess
    if not (WURZEL / ".git").exists():
        pytest.skip("keine Git-Historie")
    rel = "app/dns_provider/KATALOG_VERSION"
    try:
        commits = subprocess.run(["git", "-C", str(WURZEL), "log", "--format=%H", "--", rel],
                                 capture_output=True, text=True, check=True).stdout.split()
    except FileNotFoundError:
        pytest.skip("git nicht installiert")
    register = dict(_register())
    for c in commits:
        inhalt = subprocess.run(["git", "-C", str(WURZEL), "show", f"{c}:{rel}"],
                                capture_output=True, text=True)
        if inhalt.returncode:
            continue                                    # Commit, der die Datei löschte
        fassung, summen = "", {}
        for z in inhalt.stdout.splitlines():
            z = z.strip()
            if not z or z.startswith("#"):
                continue
            if z.startswith("version "):
                fassung = z.split(None, 1)[1]
            else:
                s, d = z.split(None, 1)
                summen[d.strip()] = s
        assert register.get(fassung) == _fingerabdruck(summen), (
            f"Commit {c[:7]}: Fassung {fassung} trug damals anderen Inhalt als FASSUNGEN heute sagt — "
            f"eine vergebene Fassung wird nie umgeschrieben, sondern hochgezählt")


def test_katalog_ohne_relay_interna():
    """Nur Standardbibliothek, httpx und relative Importe — sonst wandert er nicht ins Gateway."""
    erlaubt = set(sys.stdlib_module_names) | {"httpx"}
    for datei in KATALOG.glob("*.py"):
        for knoten in ast.walk(ast.parse(datei.read_text("utf-8"))):
            if isinstance(knoten, ast.Import):
                for a in knoten.names:
                    assert a.name.split(".")[0] in erlaubt, f"{datei.name}: import {a.name}"
            elif isinstance(knoten, ast.ImportFrom) and knoten.level == 0:
                assert knoten.module.split(".")[0] in erlaubt, f"{datei.name}: from {knoten.module}"


def test_lizenzkopf_apache_und_herkunft():
    for datei in KATALOG.glob("*.py"):
        kopf = datei.read_text("utf-8")[:400]
        assert "SPDX-License-Identifier: Apache-2.0" in kopf, datei.name
        assert "Herkunft: EXO SMTP Relay" in kopf, datei.name


def test_katalog_im_paket():
    """Windows-, Offline- und Linux-Paket nehmen dieselbe Dateiliste (update_paket._dateien)."""
    import update_paket
    im_paket = {rel for _f, rel in update_paket._dateien(WURZEL)}
    for datei in [p.name for p in KATALOG.glob("*.py")] + ["KATALOG_VERSION", "FASSUNGEN"]:
        assert f"app/dns_provider/{datei}" in im_paket, datei
