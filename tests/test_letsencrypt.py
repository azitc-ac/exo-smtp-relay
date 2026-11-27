"""Let's Encrypt für das TLS-Zertifikat (v0.2.38, wie Gateway).

Geprüft wird der Lebenszyklus in EINEM Pfad: ausstellen → erneuern → der
Listener BENUTZT das neue Zertifikat (echter STARTTLS-Handshake). certbot und
der ACME-Server sind ersetzt; Port 80, die Verzeichnisse, die Übernahme an
SMTP_TLS_CERT/KEY und das Neuladen des Listeners laufen echt.

Dazu: RSA-Schlüssel für Altgeräte, Erneuerung überschreibt kein bewusst
importiertes Zertifikat, Selbstheilung nach einer Wiederherstellung ohne
certbot-Verzeichnisse, Staging zählt nicht als CA-signiert, Band, Backup,
Oberflächenpunkt je Einstellung.
"""
import asyncio
import datetime as dt
import json
import os
import smtplib
import socket
import ssl
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

WURZEL = Path(__file__).resolve().parent.parent


# ── Zertifikate wie von Let's Encrypt ────────────────────────────────────────

def _ca(org="Let's Encrypt", cn="R11"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    jetzt = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(jetzt - dt.timedelta(days=1))
            .not_valid_after(jetzt + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _blatt(ca, host, tage, public_key=None):
    ca_key, ca_cert = ca
    key = None
    if public_key is None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = key.public_key()
    jetzt = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(ca_cert.subject).public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(jetzt - dt.timedelta(days=1))
            .not_valid_after(jetzt + dt.timedelta(days=tage))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()) if key else None
    return pem, key_pem


@pytest.fixture
def umgebung(monkeypatch, data_dir):
    import config
    import le_certbot
    import settings_store
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(data_dir / "certs" / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(data_dir / "certs" / "key.pem"))
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", data_dir / "settings.json")
    monkeypatch.setattr(settings_store, "_data", {})
    monkeypatch.setattr(config, "_ENV_SEEDS", {})
    settings_store.init({})
    # certbot wird simuliert (auch unter Windows, wo LE_METHODE sonst dns01 vorgibt):
    # diese Tests prüfen den HTTP-01-Weg.
    settings_store.update({"LE_METHODE": "http01"})
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        monkeypatch.setattr(le_certbot, "HTTP01_PORT", s.getsockname()[1])
    monkeypatch.setattr(le_certbot.shutil, "which", lambda name: "/usr/bin/certbot")
    return data_dir


class FakeCertbot:
    """Ersetzt `subprocess.run` für certbot: legt live/ und renewal/ an wie
    certbot, prüft dabei Port 80 von außen und merkt sich die Aufrufe."""

    def __init__(self, ca, host="relay.firma.de", tage=90):
        self.ca, self.host, self.tage = ca, host, tage
        self.aufrufe: list[list[str]] = []
        self.challenge_antworten: list[tuple[int, bytes]] = []

    def _flag(self, cmd, name):
        return Path(cmd[cmd.index(name) + 1])

    def _http(self, pfad):
        import le_certbot
        url = f"http://127.0.0.1:{le_certbot.HTTP01_PORT}{pfad}"
        try:
            with urllib.request.urlopen(url, timeout=5) as r:      # noqa: S310
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def __call__(self, cmd, **kw):
        self.aufrufe.append(cmd)
        conf = self._flag(cmd, "--config-dir")
        if cmd[1] == "certonly":
            webroot = self._flag(cmd, "-w")
            tok = webroot / ".well-known" / "acme-challenge" / "tok123"
            tok.parent.mkdir(parents=True, exist_ok=True)
            tok.write_bytes(b"tok123.thumb")
            self.challenge_antworten.append(self._http("/.well-known/acme-challenge/tok123"))
            self.challenge_antworten.append(
                self._http("/.well-known/acme-challenge/../../../settings.json"))
            self.challenge_antworten.append(self._http("/settings.json"))
        elif not (conf / "renewal" / "relay.conf").exists():
            return _Ergebnis(1, "", "no renewal config")
        live = conf / "live" / "relay"
        live.mkdir(parents=True, exist_ok=True)
        (conf / "renewal").mkdir(parents=True, exist_ok=True)
        (conf / "renewal" / "relay.conf").write_text("[renewalparams]\n")
        pem, key_pem = _blatt(self.ca, self.host, self.tage)
        leaf = pem.split(b"-----END CERTIFICATE-----")[0] + b"-----END CERTIFICATE-----\n"
        (live / "fullchain.pem").write_bytes(pem)
        (live / "cert.pem").write_bytes(leaf)
        (live / "privkey.pem").write_bytes(key_pem)
        return _Ergebnis(0, "", "")


class _Ergebnis:
    def __init__(self, rc, out, err):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _angeboten(port: int) -> x509.Certificate:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with smtplib.SMTP("127.0.0.1", port, timeout=10) as s:
        s.ehlo()
        s.starttls(context=ctx)
        return x509.load_der_x509_certificate(s.sock.getpeercert(binary_form=True))


@pytest.fixture
def listener(umgebung):
    import main
    import tls_cert

    class _Still:
        async def handle_DATA(self, server, session, envelope):
            return "250 OK"

    tls_cert.selbstsigniert_erzeugen("relay.firma.de")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ctl = main._LenientController(_Still(), hostname="127.0.0.1", port=port,
                                  tls_context=main._build_tls_context(), require_starttls=True)
    ctl.start()
    try:
        yield port
    finally:
        ctl.stop()


# ── HTTP-01 über certbot ─────────────────────────────────────────────────────

def test_lebenszyklus_ausstellen_erneuern_benutzen(listener, monkeypatch):
    """Ein Pfad: ausstellen → Listener nutzt es → Zeitplaner erneuert kurz vor
    Ablauf → Listener nutzt das NEUE, ohne Neustart."""
    import le_certbot
    import scheduler
    import settings_store
    ca = _ca()
    certbot = FakeCertbot(ca, tage=10)
    monkeypatch.setattr(le_certbot.subprocess, "run", certbot)

    assert _angeboten(listener).issuer == _angeboten(listener).subject   # selbstsigniert
    ok, info = le_certbot.ausstellen("relay.firma.de", "it@firma.de")
    assert ok, info
    le_certbot.lauf_vermerken(True, info, "http01")
    erstes = _angeboten(listener)
    assert erstes.issuer == ca[1].subject
    assert le_certbot.verwaltet()

    settings_store.update({"LE_RENEW_DAYS": 14, "LE_AUTO_RENEW": True})
    certbot.tage = 90
    monkeypatch.setattr(scheduler, "_le_versuch_tag", "")
    assert scheduler._tls_pruefen() == "renewed"
    zweites = _angeboten(listener)
    assert zweites.serial_number != erstes.serial_number
    assert zweites.not_valid_after_utc > erstes.not_valid_after_utc + dt.timedelta(days=60)
    assert (settings_store.get("LE_LAUF") or {}).get("ok") is True
    # renew mit DENSELBEN Verzeichnissen wie certonly (der Gateway-Fehler)
    flags = [c[c.index("--config-dir"):c.index("--config-dir") + 6] for c in certbot.aufrufe]
    assert flags[0] == flags[1]
    assert "--no-random-sleep-on-renew" in certbot.aufrufe[1]


def test_port_80_nur_challenge_und_nur_waehrend_des_laufs(umgebung, monkeypatch):
    import le_certbot
    (umgebung / "settings.json").write_text('{"SUBMIT_PASSWORD": "geheim"}')
    certbot = FakeCertbot(_ca())
    monkeypatch.setattr(le_certbot.subprocess, "run", certbot)
    assert le_certbot.ausstellen("relay.firma.de", "it@firma.de")[0]
    (ok, inhalt), (trav, _), (fremd, _) = certbot.challenge_antworten
    assert ok == 200 and inhalt == b"tok123.thumb"
    assert trav == 404 and fremd == 404, "nichts außerhalb des Webroots"
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", le_certbot.HTTP01_PORT), timeout=1).close()


def test_rsa_schluessel_fuer_altgeraete(umgebung, monkeypatch):
    """certbot nimmt seit 2.0 ECDSA — damit bekäme ein Gerät, das nur
    statischen RSA-Schlüsseltausch kann, keinen Handshake."""
    import le_certbot
    certbot = FakeCertbot(_ca())
    monkeypatch.setattr(le_certbot.subprocess, "run", certbot)
    le_certbot.ausstellen("relay.firma.de", "it@firma.de")
    cmd = certbot.aufrufe[0]
    assert cmd[cmd.index("--key-type") + 1] == "rsa"


def test_uebernahme_rechte_des_schluessels(umgebung, monkeypatch):
    import config
    import le_certbot
    monkeypatch.setattr(le_certbot.subprocess, "run", FakeCertbot(_ca()))
    le_certbot.ausstellen("relay.firma.de", "it@firma.de")
    live = umgebung / "le-config" / "live" / "relay"
    assert Path(config.SMTP_TLS_CERT).read_bytes() == (live / "fullchain.pem").read_bytes()
    assert Path(config.SMTP_TLS_KEY).read_bytes() == (live / "privkey.pem").read_bytes()
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(config.SMTP_TLS_KEY).st_mode) == 0o600


def test_erneuerung_ueberschreibt_kein_importiertes_zertifikat(umgebung, monkeypatch):
    """Nach certbot ein PFX importiert: Die Renewal-Konfiguration bleibt, aber
    der Listener liefert nicht certbots Zertifikat → nicht anfassen."""
    import le_certbot
    import tls_cert
    monkeypatch.setattr(le_certbot.subprocess, "run", FakeCertbot(_ca()))
    le_certbot.ausstellen("relay.firma.de", "it@firma.de")
    assert le_certbot.verwaltet()
    tls_cert.selbstsigniert_erzeugen("relay.firma.de")      # steht für „anderes Zertifikat“
    vorher = (umgebung / "certs" / "cert.pem").read_bytes()
    assert not le_certbot.verwaltet()
    assert le_certbot.erneuern(force=True) == ("not_managed", "")
    assert (umgebung / "certs" / "cert.pem").read_bytes() == vorher


def test_nach_wiederherstellung_ohne_certbot_konfig_neu_ausstellen(umgebung, monkeypatch):
    """Das Backup enthält le-config/ nicht (Symlinks). Läuft das Zertifikat ab
    und war es über HTTP-01 ausgestellt, stellt der Zeitplaner neu aus."""
    import le_certbot
    import scheduler
    import settings_store
    import tls_cert
    ca = _ca()
    pem, key = _blatt(ca, "relay.firma.de", 5)
    tls_cert._schreiben(pem, key)
    settings_store.update({"LE_DOMAIN": "relay.firma.de", "LE_EMAIL": "it@firma.de",
                           "LE_LAUF": {"weg": "http01", "ok": True}})
    certbot = FakeCertbot(ca)
    monkeypatch.setattr(le_certbot.subprocess, "run", certbot)
    monkeypatch.setattr(scheduler, "_le_versuch_tag", "")
    assert scheduler._tls_pruefen() == "renewed"
    assert certbot.aufrufe[0][1] == "certonly"
    assert tls_cert.info()["tage_rest"] > 80


def test_dns01_zertifikat_erneuert_niemand_selbst(umgebung, monkeypatch):
    import le_certbot
    import scheduler
    import settings_store
    import tls_cert
    pem, key = _blatt(_ca(), "relay.firma.de", 5)
    tls_cert._schreiben(pem, key)
    settings_store.update({"LE_DOMAIN": "relay.firma.de", "LE_LAUF": {"weg": "dns01"}})
    certbot = FakeCertbot(_ca())
    monkeypatch.setattr(le_certbot.subprocess, "run", certbot)
    monkeypatch.setattr(scheduler, "_le_versuch_tag", "")
    assert scheduler._tls_pruefen() == "manuell"
    assert certbot.aufrufe == []


def test_hoechstens_ein_versuch_je_tag(umgebung, monkeypatch):
    import le_certbot
    import scheduler
    import tls_cert
    pem, key = _blatt(_ca(), "relay.firma.de", 5)
    tls_cert._schreiben(pem, key)
    rufe = []
    monkeypatch.setattr(le_certbot, "erneuern", lambda: rufe.append(1) or ("error", "x"))
    monkeypatch.setattr(scheduler, "_le_versuch_tag", "")
    scheduler._tls_pruefen()
    assert scheduler._tls_pruefen() == "heute-schon"
    assert len(rufe) == 1


def test_ohne_certbot_klare_meldung(umgebung, monkeypatch):
    import le_certbot
    monkeypatch.setattr(le_certbot.shutil, "which", lambda name: None)
    ok, text = le_certbot.ausstellen("relay.firma.de", "it@firma.de")
    assert not ok and "DNS-01" in text


# ── DNS-01 von Hand ──────────────────────────────────────────────────────────

class FakeAcme:
    ca = None
    finalisiert: list = []

    def __init__(self, directory_url, account_key, account_url=""):
        self.account_key, self.account_url = account_key, account_url
        self._cert = None

    async def ensure_account(self, email=""):
        self.account_url = "https://acme.test/acct/1"
        return self.account_url

    async def new_order_dns(self, domain):
        return {"authorizations": ["https://acme.test/authz/1"], "finalize": "https://acme.test/fin/1",
                "order_url": "https://acme.test/order/1"}

    async def get_authorization(self, url):
        return {"challenges": [{"type": "http-01", "url": "x", "token": "h"},
                               {"type": "dns-01", "url": "https://acme.test/ch/1", "token": "tokDNS"}]}

    async def trigger_challenge(self, url):
        return {"status": "pending"}

    async def poll_order_status(self, url, timeout_sec=600):
        if FakeAcme.finalisiert:
            return {"status": "valid", "certificate": "https://acme.test/cert/1"}
        return {"status": "ready"}

    async def finalize(self, url, csr_der):
        csr = x509.load_der_x509_csr(csr_der)
        FakeAcme.finalisiert.append(csr)
        return {"status": "processing"}

    async def download_certificate(self, url):
        csr = FakeAcme.finalisiert[-1]
        host = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        return _blatt(FakeAcme.ca, host, 90, public_key=csr.public_key())[0]


def test_dns01_ablauf_rsa_und_listener(listener, monkeypatch, umgebung):
    import acme_client
    import tls_acme_dns
    monkeypatch.setattr(acme_client, "AcmeClient", FakeAcme)
    FakeAcme.ca, FakeAcme.finalisiert = _ca(), []
    rec = asyncio.run(tls_acme_dns.start("Relay.Firma.de", "it@firma.de"))
    assert rec["record_name"] == "_acme-challenge.relay.firma.de"
    pfad = umgebung / tls_acme_dns.PENDING_NAME
    p = json.loads(pfad.read_text())
    assert p["record_value"] == rec["record_value"], "Oberfläche zeigt ihn nach Neuladen wieder"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(pfad).st_mode) == 0o600
    info = asyncio.run(tls_acme_dns.finish())
    assert info["hostnames"] == ["relay.firma.de"]
    assert not pfad.exists()
    assert isinstance(FakeAcme.finalisiert[-1].public_key(), rsa.RSAPublicKey), "RSA für Altgeräte"
    assert _angeboten(listener).issuer == FakeAcme.ca[1].subject


# ── Connector, Band, Backup, Oberfläche ──────────────────────────────────────

def test_staging_zaehlt_nicht_als_ca_signiert(umgebung, monkeypatch):
    import exo_setup
    import settings_store
    import tls_cert
    settings_store.update({"CLIENT_ID": "x", "TENANT_DOMAIN": "firma.onmicrosoft.com"})
    monkeypatch.setattr(exo_setup, "_voraussetzungen", lambda: (True, ""))
    skripte = []
    monkeypatch.setattr(exo_setup, "_run", lambda args, **kw: skripte.append(args) or {"ok": True, "output": ""})
    pem, key = _blatt(_ca(org="(STAGING) Let's Encrypt", cn="(STAGING) Ersatz Edamame E1"),
                      "relay.firma.de", 90)
    tls_cert._schreiben(pem, key)
    assert tls_cert.info()["staging"] and not tls_cert.info()["ca_signiert"]
    r = exo_setup.connector_einrichten("relay.firma.de", [])
    assert not r["ok"] and "Testumgebung" in r["output"] and skripte == []
    pem, key = _blatt(_ca(), "relay.firma.de", 90)
    tls_cert._schreiben(pem, key)
    assert exo_setup.connector_einrichten("relay.firma.de", [])["ok"]
    assert len(skripte) == 1, "Let's Encrypt gilt als CA-signiert"


def test_band_bei_nahem_ablauf(umgebung):
    import settings_store
    import tls_cert
    from types import SimpleNamespace
    from webui import deps
    pem, key = _blatt(_ca(), "relay.firma.de", 30)
    tls_cert._schreiben(pem, key)
    req = SimpleNamespace(url=SimpleNamespace(path="/"))
    seite = deps.templates.env.get_template("base.html")
    assert 'id="tls-ablauf-banner"' not in seite.render(request=req, active="dashboard")
    settings_store.update({"LE_RENEW_DAYS": 45})
    html = seite.render(request=req, active="dashboard")
    assert 'id="tls-ablauf-banner"' in html and 'href="/einstellungen#tls"' in html
    assert 'tls-ablauf-banner' not in seite.render(request=req, active="login")


def test_backup_ohne_certbot_verzeichnisse_mit_dns01_zustand(umgebung, monkeypatch):
    import io
    import zipfile
    import backup_manager as bm
    import tls_cert
    monkeypatch.setattr(bm, "DATA_DIR", umgebung)
    tls_cert.selbstsigniert_erzeugen("relay.firma.de")
    for d in ("le-config/live/relay", "le-work", "le-logs", "acme-webroot"):
        (umgebung / d).mkdir(parents=True)
        (umgebung / d / "x.pem").write_text("x")
    (umgebung / "tls_dns01_pending.json").write_text("{}")
    daten, _ = bm.create_backup()
    namen = zipfile.ZipFile(io.BytesIO(daten)).namelist()
    assert not [n for n in namen if "le-" in n or "acme-webroot" in n], namen
    assert "data/tls_dns01_pending.json" in namen
    assert bm._bereich_von("tls_dns01_pending.json") == "tls"


def test_jede_le_einstellung_hat_einen_oberflaechenpunkt():
    import settings_store
    html = (WURZEL / "app" / "webui" / "templates" / "einstellungen.html").read_text("utf-8")
    schluessel = [k for k in settings_store.DEFAULTS
                  if k.startswith(("LE_", "ACME_")) and k != "LE_LAUF"]
    assert len(schluessel) == 6                  # seit 0.2.57-beta mit LE_METHODE
    for k in schluessel:
        assert f'id="{k}"' in html, k
        assert f"'{k}'" in html, f"{k} wird nicht gespeichert"
    assert "le.lauf" in html, "LE_LAUF (Zustand) wird angezeigt"


def test_acme_client_ist_gespiegelt():
    sys.path.insert(0, str(WURZEL / "tools"))
    import driftcheck
    assert ("app/acme_client.py" in [r for r, _ in driftcheck.MIRRORED])
