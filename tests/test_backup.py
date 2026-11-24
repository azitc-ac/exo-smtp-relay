"""Backup und Wiederherstellung (v0.2.35, wie Gateway f0c95af).

Kern ist der Rundlauf: einrichten → sichern → Datenverzeichnis leeren →
zurückspielen → derselbe Zustand (Einstellungen samt Geheimnissen,
Geräteliste, TLS-Paar, Anmeldezertifikat, Mail-Protokoll) und dieselben
Rechte (600/700). Dazu: Passwort, fremde Archive, Zip-Slip, Windows-Namen.
"""
import io
import json
import stat
import sys
import zipfile
from pathlib import PurePosixPath, PureWindowsPath

import pytest

import backup_manager as bm

PASSWORT = "lang-genug-1"


@pytest.fixture
def relay(monkeypatch, data_dir):
    """Ein eingerichtetes Relay in `data_dir` — über die echten Module."""
    import auth_cert
    import config
    import exo_mailboxes
    import mail_audit
    import relay_hosts
    import settings_store
    monkeypatch.setattr(bm, "DATA_DIR", data_dir)
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", data_dir / "settings.json")
    monkeypatch.setattr(settings_store, "_data", {})
    monkeypatch.setattr(relay_hosts, "DB_PATH", data_dir / "relay_hosts.db")
    monkeypatch.setattr(mail_audit, "DB_PATH", data_dir / "mail_audit.db")
    monkeypatch.setattr(mail_audit, "_initialised", False)
    monkeypatch.setattr(auth_cert, "PFX_PATH", data_dir / "auth.pfx")
    monkeypatch.setattr(exo_mailboxes, "AUTH_CERT_PATH", data_dir / "auth.pfx")
    monkeypatch.setattr(exo_mailboxes, "CACHE_PATH", data_dir / "mailboxes.json")
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(data_dir / "certs" / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(data_dir / "certs" / "key.pem"))
    monkeypatch.setattr(config, "_ENV_SEEDS", {})
    settings_store.init({})
    settings_store.update({"SETUP_COMPLETE": True, "SMTP_RELAY_ENABLED": True,
                           "PUBLIC_HOSTNAME": "relay.firma.de",
                           "SUBMIT_PASSWORD": "konto-geheim", "RECHTE_HERABGESTUFT": "2026-11-01"})
    relay_hosts.speichern("10.0.0.5", kommentar="Drucker 2. OG")
    relay_hosts.speichern("10.0.0.6", kommentar="Scanner")
    import tls_cert
    tls_cert.selbstsigniert_erzeugen("relay.firma.de")
    auth_cert.erzeugen()
    mail_audit.init_db()
    mail_audit.log_event(sender="s@firma.de", recipients=["e@firma.de"], subject="x",
                         message_id="", action="relay", quelle="10.0.0.5")
    # Wie exo_mailboxes es schreibt — eine Datei AUSSERHALB von
    # secure_io.SECRET_GLOBS: Ihre Rechte setzt nur der Wiederherstellungspfad
    # selbst, harden_tree() fasst sie nicht an.
    import secure_io
    secure_io.write_secret_json(data_dir / "mailboxes.json", {"mailboxes": [], "ts": 0})
    (data_dir / "logs").mkdir()
    (data_dir / "logs" / "app.log").write_text("laufzeit")
    return data_dir


def _zustand(d):
    import relay_hosts
    import settings_store
    settings_store.init({})
    dateien = {p.relative_to(d).as_posix(): p.read_bytes()
               for p in d.rglob("*") if p.is_file() and p.suffix not in (".db", ".bak")
               and "logs" not in p.parts}
    return {
        "einstellungen": settings_store.get_all(),
        "geraete": [(g["ip"], g["kommentar"]) for g in relay_hosts.liste()],
        "dateien": dateien,
    }


def _leeren(d):
    for p in sorted(d.rglob("*"), reverse=True):
        p.rmdir() if p.is_dir() else p.unlink()
    assert not any(d.iterdir())


@pytest.mark.parametrize("passwort", ["", PASSWORT])
def test_rundlauf_gleicher_zustand(relay, passwort):
    vorher = _zustand(relay)
    assert vorher["geraete"] and vorher["einstellungen"]["SUBMIT_PASSWORD"] == "konto-geheim"
    daten, name = bm.create_backup(passwort)
    assert name.endswith(".relaybak" if passwort else ".zip")
    _leeren(relay)
    import settings_store
    settings_store.init({})
    assert not settings_store.get("SETUP_COMPLETE")

    erg = bm.restore_backup(bm.oeffnen(daten, passwort))
    assert erg["ok"], erg
    nachher = _zustand(relay)
    assert nachher == vorher
    assert not (relay / "logs").exists(), "Logs gehören nicht ins Backup"
    import mail_audit
    mail_audit.init_db()
    assert mail_audit.query_events()[0]["quelle"] == "10.0.0.5"


@pytest.mark.skipif(sys.platform == "win32", reason="Unix-Rechte")
def test_wiederhergestellte_rechte(relay):
    daten, _ = bm.create_backup()
    _leeren(relay)
    relay.chmod(0o755)
    import os
    alt = os.umask(0o022)
    try:
        assert bm.restore_backup(daten)["ok"]
    finally:
        os.umask(alt)
    for p in relay.rglob("*"):
        modus = stat.S_IMODE(p.stat().st_mode)
        assert modus & 0o077 == 0, f"{p.relative_to(relay)}: {oct(modus)}"


def test_verschluesselt_ohne_klartext_und_nur_mit_passwort(relay):
    daten, _ = bm.create_backup(PASSWORT)
    assert bm.ist_verschluesselt(daten)
    assert b"konto-geheim" not in daten and b"PRIVATE KEY" not in daten
    with pytest.raises(bm.PasswortFehler):
        bm.oeffnen(daten, "")
    with pytest.raises(bm.PasswortFehler):
        bm.oeffnen(daten, "falsches-passwort")
    with pytest.raises(bm.PasswortFehler):
        bm.create_backup("kurz")


def test_unverschluesselt_warnt_im_archiv(relay):
    daten, _ = bm.create_backup()
    with zipfile.ZipFile(io.BytesIO(daten)) as zf:
        assert "NICHT verschlüsselt" in zf.read("README.txt").decode()
        assert json.loads(zf.read(bm.MANIFEST))["verschluesselt"] is False


def test_tls_paar_an_den_pfad_des_zielsystems(relay, tmp_path, monkeypatch):
    """Archivname fest, Ziel = config des Zielsystems (Umzug mit anderem Pfad)."""
    import config
    daten, _ = bm.create_backup()
    with zipfile.ZipFile(io.BytesIO(daten)) as zf:
        assert {bm.TLS_CERT_NAME, bm.TLS_KEY_NAME} <= set(zf.namelist())
    woanders = tmp_path / "anderswo"
    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(woanders / "c.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(woanders / "k.pem"))
    assert bm.restore_backup(daten, [bm.TLS_CERT_NAME, bm.TLS_KEY_NAME])["ok"]
    assert (woanders / "k.pem").read_bytes() == (relay / "certs" / "key.pem").read_bytes()


def test_zipnamen_unter_windows_mit_schraegstrich():
    assert bm._zip_name(PureWindowsPath("certs") / "cert.pem") == "data/certs/cert.pem"
    assert bm._zip_name(PurePosixPath("certs/cert.pem")) == "data/certs/cert.pem"


def test_inspect_gruppiert_und_schreibt_nichts(relay):
    daten, _ = bm.create_backup()
    _leeren(relay)
    erg = bm.inspect_backup(daten)
    assert erg["ok"] and not any(relay.iterdir())
    titel = {e["titel"]: e["dateien"] for e in erg["gruppen"][0]["eintraege"]}
    assert titel["Geräteliste"] == ["data/relay_hosts.db"]
    assert sorted(titel["TLS-Zertifikat des Listeners"]) == [bm.TLS_CERT_NAME, bm.TLS_KEY_NAME]


def test_auswahl_schraenkt_ein(relay):
    import settings_store
    daten, _ = bm.create_backup()
    settings_store.update({"PUBLIC_HOSTNAME": "neu.firma.de"})
    assert bm.restore_backup(daten, [])["ok"] is False
    assert bm.restore_backup(daten, ["data/relay_hosts.db"])["ok"]
    assert settings_store.get("PUBLIC_HOSTNAME") == "neu.firma.de", "Einstellungen nicht gewählt"


def _zip(namen: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, inhalt in namen.items():
            zf.writestr(n, inhalt)
    return buf.getvalue()


def test_fremdes_archiv_wird_abgelehnt(relay):
    gateway = _zip({"data/settings.json": "{}", "templates/signature.html": "<p>"})
    assert not bm.restore_backup(gateway)["ok"]
    assert "Relay-Backup" in bm.inspect_backup(gateway)["error"]
    fremd = _zip({bm.MANIFEST: json.dumps({"art": "exo-signature-gateway"}),
                  "data/settings.json": "{}"})
    assert not bm.restore_backup(fremd)["ok"]


def test_zip_slip(relay, tmp_path):
    boese = _zip({bm.MANIFEST: json.dumps({"art": bm.ART}), "data/settings.json": "{}",
                  "data/../../ausbruch.txt": "x"})
    erg = bm.restore_backup(boese)
    assert erg["ok"] and any("ungültiger Pfad" in w for w in erg["warnings"])
    assert not (tmp_path / "ausbruch.txt").exists()


# ── Routen ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch, relay):
    import config
    import login_drossel
    monkeypatch.setattr(config, "WEBUI_PASSWORD", "start-wert-1")
    monkeypatch.setattr(login_drossel, "_FEHLER", {})
    from fastapi.testclient import TestClient
    from webui.app import app
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/auth/local", json=dict(zip(("username", "password"),
                                                ("admin", "start-wert-1")))).status_code == 200
    return c


def test_seite_und_navigation(client):
    r = client.get("/backup")
    assert r.status_code == 200 and 'href="/backup" class="active"' in r.text
    assert "im Klartext" in r.text


def test_verschluesselter_abruf_genau_einmal(client):
    assert client.post("/api/backup/erstellen", json={"passwort": "kurz"}).status_code == 400
    e = client.post("/api/backup/erstellen", json={"passwort": PASSWORT}).json()
    r = client.get("/api/backup/download", params={"abruf": e["abruf"]})
    assert r.status_code == 200 and bm.ist_verschluesselt(r.content)
    assert client.get("/api/backup/download", params={"abruf": e["abruf"]}).status_code == 404

    d = client.post("/api/backup/inspect", files={"file": ("b", r.content)}).json()
    assert d["passwort_noetig"] is True
    d = client.post("/api/backup/inspect", files={"file": ("b", r.content)},
                    data={"passwort": PASSWORT}).json()
    assert d["ok"]


def test_route_rundlauf(client, relay):
    r = client.get("/api/backup/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    (relay / "relay_hosts.db").unlink()
    d = client.post("/api/backup/restore", files={"file": ("b.zip", r.content)},
                    data={"auswahl": json.dumps(["data/relay_hosts.db"])}).json()
    assert d["ok"] and d["restored_files"] == 1
    import relay_hosts
    assert {g["ip"] for g in relay_hosts.liste()} == {"10.0.0.5", "10.0.0.6"}
