"""Ausgehende TLS-Verbindungen vertrauen Systemspeicher UND certifi (0.2.43).

Labtest 0.2.42 (Windows Server 2025, frisch): „Aktualisierung prüfen“ brach
mit CERTIFICATE_VERIFY_FAILED ab — Windows lädt Wurzeln erst bei Bedarf über
CryptoAPI nach, Python sieht nur den vorhandenen Speicher. Dasselbe traf
STARTTLS zu Exchange Online (smarthost.py).
"""
import ast
import datetime as dt
import ssl

import pytest

import aktualisierung
import smarthost
import tls_vertrauen
from conftest import APP


def _ca_pem(cn: str) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    jetzt = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(jetzt - dt.timedelta(minutes=5))
            .not_valid_after(jetzt + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM)


def _subjekte(ctx: ssl.SSLContext) -> set[str]:
    out = set()
    for c in ctx.get_ca_certs():
        for rdn in c.get("subject", ()):
            for k, v in rdn:
                if k == "commonName":
                    out.add(v)
    return out


def test_kontext_laedt_certifi_zusaetzlich(tmp_path, monkeypatch):
    """Was certifi.where() liefert, steht im Kontext — und die Prüfung bleibt streng."""
    import certifi
    buendel = tmp_path / "cacert.pem"
    buendel.write_bytes(_ca_pem("certifi-Probe-Wurzel"))
    monkeypatch.setattr(certifi, "where", lambda: str(buendel))
    ctx = tls_vertrauen.client_kontext()
    assert "certifi-Probe-Wurzel" in _subjekte(ctx)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_kontext_behaelt_systemspeicher(tmp_path, monkeypatch):
    """certifi ERSETZT den Systemspeicher nicht (Firmen-CA eines internen
    Smarthosts steht nur dort): Was create_default_context lädt, bleibt."""
    import certifi
    buendel = tmp_path / "cacert.pem"
    buendel.write_bytes(_ca_pem("certifi-Probe-Wurzel"))
    monkeypatch.setattr(certifi, "where", lambda: str(buendel))
    echt = ssl.create_default_context
    monkeypatch.setattr(tls_vertrauen.ssl, "create_default_context", lambda *a, **k: _mit_system(echt, tmp_path))
    assert {"System-Probe-Wurzel", "certifi-Probe-Wurzel"} <= _subjekte(tls_vertrauen.client_kontext())


def _mit_system(echt, tmp_path):
    ctx = echt()
    p = tmp_path / "system.pem"
    p.write_bytes(_ca_pem("System-Probe-Wurzel"))
    ctx.load_verify_locations(cafile=str(p))
    return ctx


def test_aktualisierung_laedt_mit_gemeinsamem_kontext(monkeypatch):
    marke = object()
    gesehen = {}

    class _Antwort:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"{}"

    def _urlopen(req, timeout=None, context=None):
        gesehen["context"] = context
        return _Antwort()

    monkeypatch.setattr(tls_vertrauen, "client_kontext", lambda: marke)
    monkeypatch.setattr(aktualisierung.urllib.request, "urlopen", _urlopen)
    aktualisierung._laden("https://api.github.com/x")
    assert gesehen["context"] is marke


@pytest.mark.parametrize("modus", ["smarthost", "submit"])
def test_smarthost_starttls_mit_gemeinsamem_kontext(monkeypatch, einstellungen, modus):
    marke = ssl.create_default_context()
    kontexte = []

    class _SMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            return 250, b"ok"

        def starttls(self, context=None):
            kontexte.append(context)

        def login(self, *a):
            pass

    einstellungen.update({"EXO_SUBMIT_MODE": modus, "SUBMIT_USER": "u@firma.de", "SUBMIT_PASSWORD": "x"})
    monkeypatch.setattr(tls_vertrauen, "client_kontext", lambda: marke)
    monkeypatch.setattr(smarthost.smtplib, "SMTP", _SMTP)
    monkeypatch.setattr(smarthost, "_acquire_oauth_token", lambda: None)
    assert smarthost.verbindungstest()["ok"]
    assert kontexte and all(k is marke for k in kontexte)


# ── Regel als Prüfung: jede ausgehende Verbindung nimmt den gemeinsamen Pfad ──

# acme_client.py ist Spiegeldatei des Gateways (tools/driftcheck.py) — httpx
# nimmt dort certifi; main.py baut den SERVER-Kontext der Geräteseite.
_AUSNAHMEN = {"tls_vertrauen.py", "acme_client.py"}


def _aufrufe():
    for datei in sorted(APP.rglob("*.py")):
        if datei.name in _AUSNAHMEN or "__pycache__" in datei.parts:
            continue
        baum = ast.parse(datei.read_text(encoding="utf-8"))
        for knoten in ast.walk(baum):
            if isinstance(knoten, ast.Call):
                yield datei.relative_to(APP).as_posix(), knoten


def _name(f: ast.expr) -> str:
    if isinstance(f, ast.Attribute):
        return f"{_name(f.value)}.{f.attr}"
    if isinstance(f, ast.Name):
        return f.id
    return ""


def test_keine_ausgehende_verbindung_am_gemeinsamen_kontext_vorbei():
    verstoesse = []
    for datei, k in _aufrufe():
        n = _name(k.func)
        kw = {x.arg for x in k.keywords}
        if n == "ssl.create_default_context":
            # Nur der Server-Kontext (Purpose.CLIENT_AUTH) darf eigene Wege gehen.
            if not any("CLIENT_AUTH" in ast.unparse(a) for a in k.args):
                verstoesse.append(f"{datei}:{k.lineno} {n}()")
        elif n.endswith("urlopen") and "context" not in kw:
            verstoesse.append(f"{datei}:{k.lineno} {n} ohne context=")
        elif n in ("httpx.AsyncClient", "httpx.Client", "httpx.get", "httpx.post") and "verify" not in kw:
            verstoesse.append(f"{datei}:{k.lineno} {n} ohne verify=")
    assert not verstoesse, "Ausgehendes TLS ohne tls_vertrauen.client_kontext():\n" + "\n".join(verstoesse)
