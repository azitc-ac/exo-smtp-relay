"""Aktualisierung aus fertigen Releases (v0.2.40).

Geprüft mit nachgebauten Releases (API-Antwort + Assets, echte Pakete aus
`update_paket.bauen`, echte Ed25519-Signatur mit einem Testschlüssel):

1. Kanalwahl: stable nimmt nur Fassungen ohne Suffix, beta die höchste insgesamt.
2. Rundlauf prüfen → einspielen → tauschen; Daten bleiben unberührt.
3. Fehlstart → Rückfall auf die alte Fassung.
4. Manipulierte Prüfsumme, manipuliertes Paket, fremde/fehlende Signatur,
   fehlender eingebauter Schlüssel → abgelehnt, nichts getauscht.
5. Der Tauscher prüft selbst noch einmal (vertraut dem Webprozess nicht).
6. Derselbe Ablauf mit Phasen als eigenen Prozessen (Betriebsweg).
7. Release-Workflow: nur Tag-Auslöser, Signatur vor jeder Veröffentlichung.
"""
import re
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import update_paket as up                                     # noqa: E402
import update_tausch as ut                                    # noqa: E402

from test_oberflaeche import client, _anmelden                # noqa: E402,F401

ALT_V, NEU_V = "0.2.40", "0.2.41"


# ── Bausteine ────────────────────────────────────────────────────────────────

@pytest.fixture
def schluessel(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    k = Ed25519PrivateKey.generate()
    pub = base64.b64encode(k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    monkeypatch.setattr(up, "RELEASE_SCHLUESSEL", (pub,))
    return k


def _baum(ziel: Path, version: str, main: str = "print('neu')\n", lock: str = "x==1\n") -> Path:
    (ziel / "app").mkdir(parents=True)
    (ziel / "app" / "main.py").write_text(main, encoding="utf-8")
    (ziel / "app" / "requirements.lock").write_text(lock, encoding="utf-8")
    (ziel / "windows").mkdir()
    (ziel / "windows" / "install.ps1").write_text("# install\n", encoding="utf-8")
    (ziel / "linux").mkdir()
    (ziel / "linux" / "exo-smtp-relay.service").write_text("[Unit]\n", encoding="utf-8")
    for name, inhalt in (("VERSION", version + "\n"), ("LICENSE.md", "Apache\n"),
                         ("NOTICE", "Notice\n"), ("CHANGELOG.md", f"## {version} — x\n"),
                         ("README.md", "readme\n")):
        (ziel / name).write_text(inhalt, encoding="utf-8")
    return ziel


def _release(tmp: Path, k, version=NEU_V, signieren=True) -> Path:
    dist = tmp / f"dist-{version}"
    pakete = up.bauen(_baum(tmp / f"quelle-{version}", version), dist, version)
    (dist / up.SUMMEN).write_text(up.summen_schreiben(pakete))
    if signieren:
        (dist / up.SIGNATUR).write_text(base64.b64encode(k.sign((dist / up.SUMMEN).read_bytes())).decode())
    return dist


def _api(dist: Path, version=NEU_V, prerelease=False) -> dict:
    return {"tag_name": f"v{version}", "draft": False, "prerelease": prerelease,
            "html_url": f"https://example.invalid/v{version}",
            "assets": [{"name": p.name, "browser_download_url": f"https://dl.invalid/{p.name}"}
                       for p in sorted(dist.iterdir())]}


def _baumstempel(d: Path) -> dict:
    return {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in d.rglob("*") if p.is_file() and "update" not in p.relative_to(d).parts}


@pytest.fixture
def anlage(tmp_path, monkeypatch, einstellungen):
    """Installierte Fassung 0.2.40 als Windows-Dienst mit Daten, GitHub nachgebaut."""
    import aktualisierung
    import config
    wurzel = _baum(tmp_path / "installiert", ALT_V, main="print('alt')\n")
    daten = wurzel / "data"
    (daten / "certs").mkdir(parents=True)
    (daten / "settings.json").write_text('{"GATEWAY_NAME": "x"}')
    (daten / "certs" / "key.pem").write_text("geheim")
    (daten / "relay_hosts.db").write_bytes(b"\x00db")
    ut.json_schreiben(daten / "update" / "waechter.json", {"protokoll": ut.PROTOKOLL})
    monkeypatch.setattr(config, "DATA_DIR", str(daten))
    monkeypatch.setattr(config, "VERSION", ALT_V)
    monkeypatch.setenv("RELAY_BETRIEBSART", "windows")
    einstellungen.update({"UPDATE_KANAL": "stable", "UPDATE_PRUEF_STUNDEN": 24})
    netz: dict = {}
    monkeypatch.setattr(aktualisierung, "_laden", lambda url, **kw: netz[url])

    def veroeffentlichen(dist, releases):
        netz[aktualisierung.API_RELEASES] = json.dumps(releases).encode()
        for p in dist.iterdir():
            netz[f"https://dl.invalid/{p.name}"] = p.read_bytes()
    return {"wurzel": wurzel, "daten": daten, "netz": netz, "veroeffentlichen": veroeffentlichen}


class _Dienst:
    """Nachgebauter Dienst: Ein Start der Fassung schreibt ihr Lebenszeichen —
    ausser sie ist als „startet nicht“ markiert."""

    def __init__(self, wurzel, daten, defekt=()):
        self.wurzel, self.daten, self.defekt = wurzel, daten, set(defekt)
        self.laeuft_jetzt, self.starts, self.stopps = True, [], 0

    def stoppen(self):
        self.laeuft_jetzt = False
        self.stopps += 1

    def starten(self):
        v = ut.aktuelle_version(self.wurzel)
        self.starts.append(v)
        self.laeuft_jetzt = v not in self.defekt
        if self.laeuft_jetzt:
            ut.lebenszeichen_schreiben(self.daten, v)

    def laeuft(self):
        return self.laeuft_jetzt


def _ablauf(a, dienst, phase=None):
    uhr = [0.0]

    def schlafen(s):
        uhr[0] += s
    return ut.ablauf(a["wurzel"], a["daten"], dienst.stoppen, dienst.starten, dienst.laeuft,
                     phase=phase or ut.phasen_im_prozess(a["wurzel"], a["daten"], NEU_V,
                                                        up.paket_name(NEU_V, "windows")),
                     warte_s=30, stabil_s=5, schlafen=schlafen, uhr=lambda: uhr[0])


# ── 1. Versionen und Kanäle ──────────────────────────────────────────────────

def test_versionsreihenfolge_und_kanal():
    reihe = ["0.2.9", "0.2.40-beta", "0.2.40-beta.1", "0.2.40-beta.2", "0.2.40", "0.2.41-beta.1", "0.3.0"]
    assert sorted(reihe, key=up.version_schluessel) == reihe
    assert up.kanal_von("0.2.40") == "stable" and up.kanal_von("0.2.40-beta.3") == "beta"
    assert up.version_schluessel("0.2.40-rc1") is None and up.kanal_von("latest") is None


def test_kanalwahl():
    import aktualisierung
    rel = [{"tag_name": t, "draft": d, "prerelease": p} for t, d, p in (
        ("v0.2.40", False, False), ("v0.2.41-beta.1", False, True),
        ("v0.2.41-beta.2", False, True), ("v0.3.0", True, False),       # Entwurf
        ("v0.2.42", False, True),                                        # als Vorab markiert
        ("nightly", False, False), ("v9.9.9-rc1", False, False))]
    assert aktualisierung.neuestes_release(rel, "stable")["tag_name"] == "v0.2.40"
    assert aktualisierung.neuestes_release(rel, "beta")["tag_name"] == "v0.2.42"
    ohne_42 = [r for r in rel if r["tag_name"] != "v0.2.42"]
    assert aktualisierung.neuestes_release(ohne_42, "beta")["tag_name"] == "v0.2.41-beta.2"
    # Die stabile Fassung schlägt ihre eigenen Betas auch im Beta-Kanal.
    ohne_42.append({"tag_name": "v0.2.41", "draft": False, "prerelease": False})
    assert aktualisierung.neuestes_release(ohne_42, "beta")["tag_name"] == "v0.2.41"
    assert aktualisierung.neuestes_release([], "stable") is None


def test_pruefen_merkt_stand_und_band(anlage, schluessel, einstellungen, tmp_path):
    import aktualisierung
    dist = _release(tmp_path, schluessel)
    beta = _api(dist, "0.2.42-beta.1", prerelease=True)
    anlage["veroeffentlichen"](dist, [_api(dist), beta])
    s = aktualisierung.pruefen()
    assert s["ok"] and s["neueste"] == NEU_V and s["verfuegbar"] is True
    assert aktualisierung.verfuegbare_version() == NEU_V
    # Kanalwechsel macht den gemerkten Stand ungültig, bis neu geprüft ist.
    einstellungen["UPDATE_KANAL"] = "beta"
    assert aktualisierung.verfuegbare_version() is None
    assert aktualisierung.pruefen()["neueste"] == "0.2.42-beta.1"


# ── 2./3. Rundlauf und Rückfall ──────────────────────────────────────────────

def test_rundlauf_pruefen_einspielen_tauschen(anlage, schluessel, tmp_path):
    import aktualisierung
    dist = _release(tmp_path, schluessel)
    anlage["veroeffentlichen"](dist, [_api(dist)])
    assert aktualisierung.pruefen()["verfuegbar"]
    vorher = _baumstempel(anlage["daten"])
    r = aktualisierung.einspielen("admin")
    assert r == {"ok": True, "version": NEU_V}, r
    assert ut.json_lesen(ut.auftrag_pfad(anlage["daten"]))["version"] == NEU_V

    dienst = _Dienst(anlage["wurzel"], anlage["daten"])
    assert _ablauf(anlage, dienst) == "ok"
    w = anlage["wurzel"]
    assert ut.aktuelle_version(w) == NEU_V and (w / "app" / "main.py").read_text(encoding="utf-8") == "print('neu')\n"
    assert (w / ut.ALT / "app" / "main.py").read_text(encoding="utf-8") == "print('alt')\n", "Rückfallstand bleibt"
    assert dienst.stopps == 1 and dienst.starts == [NEU_V]
    assert _baumstempel(anlage["daten"]) == vorher, "Daten unberührt"
    assert not ut.auftrag_pfad(anlage["daten"]).exists()
    assert not (anlage["daten"] / "update" / up.paket_name(NEU_V, "windows")).exists()
    assert ut.json_lesen(ut.status_pfad(anlage["daten"]))["zustand"] == "ok"


def test_fehlstart_rollt_zurueck(anlage, schluessel, tmp_path):
    import aktualisierung
    dist = _release(tmp_path, schluessel)
    anlage["veroeffentlichen"](dist, [_api(dist)])
    aktualisierung.pruefen()
    vorher = _baumstempel(anlage["daten"])
    assert aktualisierung.einspielen("admin")["ok"]
    dienst = _Dienst(anlage["wurzel"], anlage["daten"], defekt={NEU_V})
    assert _ablauf(anlage, dienst) == "zurueckgerollt"
    w = anlage["wurzel"]
    assert ut.aktuelle_version(w) == ALT_V and (w / "app" / "main.py").read_text(encoding="utf-8") == "print('alt')\n"
    assert dienst.starts == [NEU_V, ALT_V] and dienst.laeuft()
    assert not (w / ut.ALT).exists() and not (w / ut.NEU).exists()
    assert _baumstempel(anlage["daten"]) == vorher, "Daten unberührt"
    st = ut.json_lesen(ut.status_pfad(anlage["daten"]))
    assert st["zustand"] == "zurueckgerollt" and ALT_V in st["text"]


def test_rueckfall_entfernt_neu_hinzugekommene_teile(anlage, schluessel, tmp_path):
    """Eine Fassung ohne linux/ wird installiert; das Release bringt es mit —
    beim Rückfall muss es wieder verschwinden."""
    import aktualisierung
    shutil.rmtree(anlage["wurzel"] / "linux")
    dist = _release(tmp_path, schluessel)
    anlage["veroeffentlichen"](dist, [_api(dist)])
    aktualisierung.pruefen()
    assert aktualisierung.einspielen("admin")["ok"]
    assert _ablauf(anlage, _Dienst(anlage["wurzel"], anlage["daten"], defekt={NEU_V})) == "zurueckgerollt"
    assert not (anlage["wurzel"] / "linux").exists()


# ── 4. Manipulation ──────────────────────────────────────────────────────────

def _einspielen_mit(anlage, tmp_path, schluessel, veraendern):
    import aktualisierung
    dist = _release(tmp_path, schluessel)
    veraendern(dist)
    anlage["veroeffentlichen"](dist, [_api(dist)])
    aktualisierung.pruefen()
    r = aktualisierung.einspielen("admin")
    assert not r["ok"], "manipuliertes Release darf nicht angenommen werden"
    assert not ut.auftrag_pfad(anlage["daten"]).exists()
    assert ut.aktuelle_version(anlage["wurzel"]) == ALT_V
    return r["error"]


def test_manipulierte_pruefsumme_abgelehnt(anlage, schluessel, tmp_path):
    def falsche_summe(dist):
        s = (dist / up.SUMMEN).read_text(encoding="utf-8")
        (dist / up.SUMMEN).write_text(("0" * 64) + s[64:])
    assert "Signatur" in _einspielen_mit(anlage, tmp_path, schluessel, falsche_summe)


def test_ausgetauschtes_paket_abgelehnt(anlage, schluessel, tmp_path):
    def paket_tauschen(dist):
        p = dist / up.paket_name(NEU_V, "windows")
        p.write_bytes(p.read_bytes() + b"x")
    assert "Prüfsumme" in _einspielen_mit(anlage, tmp_path, schluessel, paket_tauschen)


def test_fremde_signatur_abgelehnt(anlage, schluessel, tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    fremd = Ed25519PrivateKey.generate()

    def fremd_signieren(dist):
        (dist / up.SIGNATUR).write_text(base64.b64encode(fremd.sign((dist / up.SUMMEN).read_bytes())).decode())
    assert "Signatur" in _einspielen_mit(anlage, tmp_path, schluessel, fremd_signieren)


def test_fehlende_signatur_abgelehnt(anlage, schluessel, tmp_path):
    assert "Signatur" in _einspielen_mit(anlage, tmp_path, schluessel,
                                         lambda dist: (dist / up.SIGNATUR).unlink())


def test_ohne_eingebauten_schluessel_nichts_einspielen(anlage, schluessel, tmp_path, monkeypatch):
    import aktualisierung
    dist = _release(tmp_path, schluessel)
    monkeypatch.setattr(up, "RELEASE_SCHLUESSEL", ())
    with pytest.raises(up.PaketFehler, match="Kein Prüfschlüssel"):
        up.signatur_pruefen((dist / up.SUMMEN).read_bytes(), (dist / up.SIGNATUR).read_bytes())
    anlage["veroeffentlichen"](dist, [_api(dist)])
    aktualisierung.pruefen()
    assert not aktualisierung.einspielen("admin")["ok"]


# ── 5. Der Tauscher prüft selbst ─────────────────────────────────────────────

def _auftrag_direkt(anlage, dist, version=NEU_V):
    """Am Webprozess vorbei: Dateien und Auftrag direkt in data/update/."""
    b = ut.bereich(anlage["daten"])
    for p in dist.iterdir():
        shutil.copy(p, b / p.name)
    ut.json_schreiben(ut.auftrag_pfad(anlage["daten"]),
                      {"version": version, "paket": up.paket_name(version, "windows")})


def test_tauscher_lehnt_manipuliertes_paket_ab(anlage, schluessel, tmp_path):
    """Ein GÜLTIG gebautes, aber anderes Paket unter dem signierten Namen —
    nur die Prüfung im Tauscher selbst hält es auf."""
    dist = _release(tmp_path, schluessel)
    fremd = tmp_path / "fremd"
    up.bauen(_baum(tmp_path / "fremd-quelle", NEU_V, main="print('fremd')\n"), fremd, NEU_V)
    shutil.copy(fremd / up.paket_name(NEU_V, "windows"), dist / up.paket_name(NEU_V, "windows"))
    _auftrag_direkt(anlage, dist)
    dienst = _Dienst(anlage["wurzel"], anlage["daten"])
    assert _ablauf(anlage, dienst) == "fehler"
    assert dienst.stopps == 0, "vor der Prüfung wird der Dienst nicht angehalten"
    assert ut.aktuelle_version(anlage["wurzel"]) == ALT_V
    assert (anlage["wurzel"] / "app" / "main.py").read_text(encoding="utf-8") == "print('alt')\n"
    assert "Prüfsumme" in ut.json_lesen(ut.status_pfad(anlage["daten"]))["text"]
    assert not ut.auftrag_pfad(anlage["daten"]).exists(), "Auftrag weg — keine Endlosschleife"


def test_tauscher_lehnt_aeltere_fassung_ab(anlage, schluessel, tmp_path):
    dist = _release(tmp_path, schluessel, version="0.2.39")
    _auftrag_direkt(anlage, dist, version="0.2.39")
    dienst = _Dienst(anlage["wurzel"], anlage["daten"])
    phase = ut.phasen_im_prozess(anlage["wurzel"], anlage["daten"], "0.2.39",
                                 up.paket_name("0.2.39", "windows"))
    assert _ablauf(anlage, dienst, phase) == "fehler"
    assert "nicht neuer" in ut.json_lesen(ut.status_pfad(anlage["daten"]))["text"]


def test_entpacken_lehnt_pfadausbruch_und_links_ab(tmp_path):
    kopf = f"exo-smtp-relay-{NEU_V}"
    z = tmp_path / "boese.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr(f"{kopf}/app/../../../aussen.txt", "x")
    with pytest.raises(up.PaketFehler, match="Unzulässiger Pfad"):
        up.entpacken(z, tmp_path / "ziel1", NEU_V)
    assert not (tmp_path.parent / "aussen.txt").exists()
    t = tmp_path / "boese.tar.gz"
    with tarfile.open(t, "w:gz") as f:
        info = tarfile.TarInfo(f"{kopf}/app/link")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        f.addfile(info)
    with pytest.raises(up.PaketFehler, match="reguläre Datei"):
        up.entpacken(t, tmp_path / "ziel2", NEU_V)
    z2 = tmp_path / "fremd.zip"
    with zipfile.ZipFile(z2, "w") as f:
        f.writestr(f"{kopf}/data/settings.json", "{}")
    with pytest.raises(up.PaketFehler, match="Unerwarteter"):
        up.entpacken(z2, tmp_path / "ziel3", NEU_V)


# ── 6. Betriebsweg: Phasen als eigene Prozesse ───────────────────────────────

def test_ablauf_mit_phasen_als_prozess(anlage, schluessel, tmp_path):
    """Wie im Dienst: Phasen laufen aus der Arbeitskopie der INSTALLIERTEN
    Fassung — mit deren eingebautem Schlüssel."""
    app = anlage["wurzel"] / "app"
    shutil.copy(WURZEL / "app" / "update_tausch.py", app / "update_tausch.py")
    text = (WURZEL / "app" / "update_paket.py").read_text(encoding="utf-8")
    # Eingebaute Schlüssel (leer oder mehrzeilig) durch den Testschlüssel ersetzen
    text, n = re.subn(r"RELEASE_SCHLUESSEL: tuple\[str, \.\.\.\] = \((?:\)|.*?\n\))",
                      f"RELEASE_SCHLUESSEL: tuple[str, ...] = ({up.RELEASE_SCHLUESSEL[0]!r},)",
                      text, count=1, flags=re.S)
    assert n == 1
    assert up.RELEASE_SCHLUESSEL[0] in text
    (app / "update_paket.py").write_text(text, encoding="utf-8")
    dist = _release(tmp_path, schluessel)
    _auftrag_direkt(anlage, dist)
    dienst = _Dienst(anlage["wurzel"], anlage["daten"])
    uhr = [0.0]
    ende = ut.ablauf(anlage["wurzel"], anlage["daten"], dienst.stoppen, dienst.starten, dienst.laeuft,
                     warte_s=30, stabil_s=5, schlafen=lambda s: uhr.__setitem__(0, uhr[0] + s),
                     uhr=lambda: uhr[0])
    assert ende == "ok", ut.json_lesen(ut.status_pfad(anlage["daten"]))
    assert ut.aktuelle_version(anlage["wurzel"]) == NEU_V
    assert not (anlage["wurzel"] / ut.ARBEIT).exists()


# ── Betriebsarten, Oberfläche, Einstellungen ─────────────────────────────────

def test_docker_tauscht_nicht_selbst(anlage, schluessel, tmp_path, monkeypatch):
    import aktualisierung
    monkeypatch.setenv("RELAY_BETRIEBSART", "docker")
    dist = _release(tmp_path, schluessel)
    anlage["veroeffentlichen"](dist, [_api(dist)])
    aktualisierung.pruefen()
    r = aktualisierung.einspielen("admin")
    assert not r["ok"] and "Container" in r["error"]
    z = aktualisierung.zustand()
    assert z["docker"]["befehl"] == "docker compose pull && docker compose up -d"
    assert z["docker"]["image"] == "ghcr.io/azitc-ac/exo-smtp-relay:stable"


def test_alter_windows_wrapper_wird_erkannt(anlage, monkeypatch):
    import aktualisierung
    (anlage["daten"] / "update" / "waechter.json").unlink()
    bereit, hinweis = aktualisierung.tauscher_bereit("windows")
    assert not bereit and "install.ps1" in hinweis


def test_jede_update_einstellung_hat_oberflaechenpunkt():
    import settings_store
    html = (WURZEL / "app" / "webui" / "templates" / "aktualisierung.html").read_text("utf-8")
    schluessel = [k for k in settings_store.DEFAULTS if k.startswith("UPDATE_")]
    assert schluessel == ["UPDATE_KANAL", "UPDATE_PRUEF_STUNDEN"]
    for k in schluessel:
        assert f'id="{k}"' in html, k
        assert f"{k}:" in html.split("async function einstSpeichern")[1], f"{k} wird nicht gespeichert"


def test_seite_und_ungueltige_werte(client):
    _anmelden(client)
    r = client.get("/aktualisierung")
    assert r.status_code == 200 and 'id="UPDATE_KANAL"' in r.text
    assert client.post("/api/einstellungen", json={"UPDATE_KANAL": "nightly"}).status_code == 400
    assert client.post("/api/einstellungen", json={"UPDATE_PRUEF_STUNDEN": "5"}).status_code == 400
    assert client.post("/api/einstellungen", json={"UPDATE_KANAL": "beta",
                                                   "UPDATE_PRUEF_STUNDEN": "6"}).json()["ok"]


def test_band_aktualisierung_verfuegbar(monkeypatch):
    from types import SimpleNamespace
    from webui import deps
    g = deps.templates.env.globals
    for name in ("relay_aus_gemeldet", "adressen_gemeldet", "password_change_required"):
        monkeypatch.setitem(g, name, lambda: False)
    monkeypatch.setitem(g, "zustellfehler_anzahl", lambda: 0)
    monkeypatch.setitem(g, "lernmodus_rest_min", lambda: 0)
    monkeypatch.setitem(g, "tls_ablauf_tage", lambda: None)
    req = SimpleNamespace(url=SimpleNamespace(path="/"))
    tpl = deps.templates.env.get_template("base.html")
    monkeypatch.setitem(g, "update_verfuegbar", lambda: None)
    assert 'id="update-banner"' not in tpl.render(request=req, active="dashboard")
    monkeypatch.setitem(g, "update_verfuegbar", lambda: NEU_V)
    html = tpl.render(request=req, active="dashboard")
    assert 'id="update-banner"' in html and f"v{NEU_V}" in html and 'href="/aktualisierung"' in html
    assert 'id="update-banner"' not in tpl.render(request=req, active="login")


def test_planmaessig_pruefen_haelt_intervall(anlage, schluessel, tmp_path, einstellungen):
    import aktualisierung
    dist = _release(tmp_path, schluessel)
    anlage["veroeffentlichen"](dist, [_api(dist)])
    assert aktualisierung.planmaessig_pruefen() is True
    assert aktualisierung.planmaessig_pruefen() is False, "innerhalb des Intervalls nicht erneut"
    einstellungen["UPDATE_PRUEF_STUNDEN"] = 0
    (anlage["daten"] / "update" / "stand.json").unlink()
    assert aktualisierung.planmaessig_pruefen() is False, "0 = nie"


def test_update_bereich_nicht_im_backup():
    import backup_manager
    from pathlib import PurePath
    assert backup_manager._ausgeschlossen(PurePath("update/exo-smtp-relay-1.0.0-windows.zip"))


# ── 7. Pakete und Release-Workflow ───────────────────────────────────────────

def test_echtes_paket_enthaelt_lizenz_notice_und_installer(tmp_path):
    version = (WURZEL / "VERSION").read_text(encoding="utf-8").strip()
    zip_p, tar_p = up.bauen(WURZEL, tmp_path, version)
    kopf = f"exo-smtp-relay-{version}/"
    namen = zipfile.ZipFile(zip_p).namelist()
    for pflicht in ("LICENSE.md", "NOTICE", "VERSION", "app/main.py", "app/update_tausch.py",
                    "windows/install.ps1", "windows/service.py",
                    "linux/exo-smtp-relay-update.path", "linux/exo-smtp-relay-update.service"):
        assert kopf + pflicht in namen, pflicht
    assert not [n for n in namen if "__pycache__" in n or n.endswith(".pyc") or "/data/" in n]
    assert sorted(tarfile.open(tar_p).getnames()) == sorted(namen)
    # Reproduzierbar: zweimal gebaut, dieselben Bytes.
    zip2, tar2 = up.bauen(WURZEL, tmp_path / "2", version)
    assert zip2.read_bytes() == zip_p.read_bytes() and tar2.read_bytes() == tar_p.read_bytes()


def test_installer_kopiert_lizenz_und_notice():
    ps = (WURZEL / "windows" / "install.ps1").read_text(encoding="utf-8-sig")
    assert '"LICENSE.md", "NOTICE"' in ps
    assert "COPY LICENSE.md NOTICE" in (WURZEL / "Dockerfile").read_text(encoding="utf-8")


def test_release_workflow_nur_durch_tag():
    yml = (WURZEL / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    kopf = yml.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert kopf.strip() == 'push:\n    tags: ["v*"]', kopf
    code = "\n".join(z for z in yml.splitlines() if not z.lstrip().startswith("#"))
    assert "workflow_dispatch" not in code and "branches" not in code
    # Signatur und Gegenprobe im ersten Job; alles Veröffentlichende hängt daran.
    pakete = yml.split("\n  image:", 1)[0]
    assert "release.py signieren" in pakete and "release.py pruefen" in pakete
    assert "secrets.RELEASE_SIGNING_KEY" in pakete
    assert "needs: pakete" in yml and "needs: [pakete, image]" in yml and "needs: [pakete, manifest, offline]" in yml
    for andere in (WURZEL / ".github" / "workflows").glob("*.yml"):
        if andere.name != "release.yml":
            t = andere.read_text(encoding="utf-8")
            assert "ghcr.io" not in t and "gh release" not in t, andere.name


def _release_tool(*args, env=None):
    return subprocess.run([sys.executable, str(WURZEL / "tools" / "release.py"), *args],
                          capture_output=True, text=True, env=env)


def test_signieren_ohne_secret_bricht_ab(tmp_path):
    (tmp_path / up.SUMMEN).write_text("0" * 64 + "  x\n")
    env = {k: v for k, v in os.environ.items() if k != "RELEASE_SIGNING_KEY"}
    r = _release_tool("signieren", str(tmp_path / up.SUMMEN), env=env)
    assert r.returncode != 0 and "RELEASE_SIGNING_KEY" in r.stderr
    assert not (tmp_path / up.SIGNATUR).exists()


def test_signieren_und_pruefen_rund(tmp_path, schluessel):
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
    dist = _release(tmp_path, schluessel, signieren=False)
    pem = schluessel.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    r = _release_tool("signieren", str(dist / up.SUMMEN), env={**os.environ, "RELEASE_SIGNING_KEY": pem})
    assert r.returncode == 0, r.stderr
    up.paket_pruefen(dist / up.paket_name(NEU_V, "linux"), (dist / up.SUMMEN).read_bytes(),
                     (dist / up.SIGNATUR).read_bytes())
    # Gegenprobe der CI gegen die EINGEBAUTEN Schlüssel: Der Testschlüssel ist
    # keiner davon, also muss sie scheitern — ein Release, das mit einem nicht
    # eingebauten Schlüssel signiert ist, bricht ab.
    r = _release_tool("pruefen", str(dist))
    assert r.returncode != 0 and "Signatur" in r.stderr, r.stderr


def test_tag_muss_zu_version_passen():
    version = (WURZEL / "VERSION").read_text(encoding="utf-8").strip()
    r = _release_tool("kanal", f"v{version}")
    assert r.returncode == 0 and f"version={version}" in r.stdout and "kanal=" in r.stdout
    assert _release_tool("kanal", "v0.0.1").returncode != 0
    assert _release_tool("kanal", f"v{version}-rc1").returncode != 0


@pytest.mark.skipif(sys.platform != "win32", reason="Sperrsemantik offener Dateien nur unter Windows")
def test_os_replace_ordner_mit_offener_datei_atomar(tmp_path):
    """Labtest 0.2.50: PowerShells Move-Item verschiebt einen Ordner mit offener Datei TEILWEISE.
    update_tausch verschiebt mit os.replace (MoveFileExW, ein Rename): Es scheitert ganz und
    bewegt nichts. Python öffnet Dateien unter Windows ohne FILE_SHARE_DELETE — wie die
    Labtest-Sperre (Share Read, kein Delete)."""
    quelle = tmp_path / "app"
    (quelle / "sub").mkdir(parents=True)
    for i in range(60):
        (quelle / ("sub" if i % 2 else ".") / f"f{i:02}.py").write_text(str(i))
    vorher = sorted(p.relative_to(quelle).as_posix() for p in quelle.rglob("*") if p.is_file())
    offen = open(quelle / "f30.py", "rb")
    try:
        with pytest.raises(OSError):
            os.replace(quelle, tmp_path / "app.neu")
    finally:
        offen.close()
    assert not (tmp_path / "app.neu").exists()
    assert sorted(p.relative_to(quelle).as_posix() for p in quelle.rglob("*") if p.is_file()) == vorher


def test_zurueck_nur_fuer_die_marke_des_eigenen_tauschs(tmp_path):
    """Scheitert das Wegräumen des VORIGEN Rückfallstands, liegt dessen Marke noch da. Die
    Rücknahme darf sie nicht für den eigenen Tausch halten (sonst käme die vorvorige Fassung)."""
    w = _baum(tmp_path / "w", "0.2.50")
    alt = w / ut.ALT
    (alt / "app").mkdir(parents=True)
    (alt / "app" / "main.py").write_text("vorvorig")
    ut.json_schreiben(alt / ut.MARKE, {"verschoben": ["app"], "neu": [], "fertig": True, "nach": "0.2.50",
                                       "lauf": "lauf-a"})
    assert "nichts zurückgenommen" in ut.phase_zurueck(w, "lauf-b")
    assert "nichts zurückgenommen" in ut.phase_zurueck(w, "")
    assert (w / "app" / "main.py").read_text(encoding="utf-8") == "print('neu')\n"
    assert "zurückgesetzt" in ut.phase_zurueck(w, "lauf-a")
    assert (w / "app" / "main.py").read_text(encoding="utf-8") == "vorvorig"
    assert not [p for p in w.iterdir() if ".relay-verworfen" in p.name], "Beiseitegelegtes aufgeräumt"


def test_tauscher_gleiche_fassung_kein_tausch_und_rueckfall_nie_ergaenzend(anlage, schluessel, tmp_path):
    """Labtest 0.2.51 Z4 (install.ps1): gleiche Fassung, kaputt — „ergänzt“ statt zurückgerollt.
    Der Updater kann diesen Pfad nicht nehmen: Er tauscht gleiche Fassungen gar nicht erst
    (phase_pruefen), und seine Rücknahme (phase_zurueck) legt das Neue immer GANZ beiseite und
    holt das Alte GANZ zurück — es gibt dort kein Ergänzen."""
    dist = _release(tmp_path, schluessel, version=ALT_V)
    _auftrag_direkt(anlage, dist, version=ALT_V)
    dienst = _Dienst(anlage["wurzel"], anlage["daten"])
    phase = ut.phasen_im_prozess(anlage["wurzel"], anlage["daten"], ALT_V, up.paket_name(ALT_V, "windows"))
    assert _ablauf(anlage, dienst, phase) == "fehler"
    assert "nicht neuer" in ut.json_lesen(ut.status_pfad(anlage["daten"]))["text"]
    assert dienst.stopps == 0
    # Rücknahme mit gleicher Zielversion in der Marke: das Neue fliegt vollständig raus.
    w = anlage["wurzel"]
    alt = w / ut.ALT
    (alt / "app").mkdir(parents=True)
    (alt / "app" / "main.py").write_text("alt")
    (alt / "app" / "nur_alt.py").write_text("alt")
    (w / "app" / "nur_neu.py").write_text("neu")
    ut.json_schreiben(alt / ut.MARKE, {"verschoben": ["app"], "neu": [], "fertig": True, "nach": ALT_V,
                                       "lauf": "x1"})
    ut.phase_zurueck(w, "x1")
    assert sorted(p.name for p in (w / "app").iterdir()) == ["main.py", "nur_alt.py"]
    assert (w / "app" / "main.py").read_text() == "alt"



def _sperre(monkeypatch, pfad_als_quelle):
    """os.replace scheitert, wenn `pfad_als_quelle` verschoben werden soll (Sperre unter Windows)."""
    echt = ut.os.replace
    gesperrt = {"an": True}

    def replace(a, b):
        if gesperrt["an"] and Path(a) == Path(pfad_als_quelle):
            raise PermissionError(13, "Der Prozess kann nicht auf die Datei zugreifen", str(a))
        return echt(a, b)
    monkeypatch.setattr(ut.os, "replace", replace)
    return gesperrt


@pytest.mark.parametrize("alte_marke", [
    {"verschoben": ["app", "windows"], "neu": [], "fertig": True, "nach": "0.2.39"},            # Updater ≤ 0.2.53
    {"verschoben": ["app", "windows"], "neu": [], "fertig": True, "nach": "0.2.39", "lauf": "fremd"},
])
def test_alter_rueckfallstand_ohne_eigene_lauf_id_wird_nie_zurueckgenommen(anlage, schluessel, tmp_path,
                                                                         monkeypatch, alte_marke):
    """Labtest 0.2.53 l6: Sperre in .update-alt\\windows — tauschen scheitert beim Beiseitelegen
    des ALTEN .update-alt, bevor der neue Merker steht. Bisher nahm die Rücknahme den alten Merker
    (ohne „nach“ = „eigener“ Tausch) und holte app\\ aus der vorvorigen Fassung. Jetzt: nichts."""
    w, daten = anlage["wurzel"], anlage["daten"]
    alt = w / ut.ALT
    for teil in ("app", "windows"):
        (alt / teil).mkdir(parents=True)
        (alt / teil / "vorvorig.txt").write_text("vorvorig")
    ut.json_schreiben(alt / ut.MARKE, alte_marke)
    vorher = _baumstempel(w)
    _sperre(monkeypatch, alt)
    dist = _release(tmp_path, schluessel)
    _auftrag_direkt(anlage, dist)
    dienst = _Dienst(w, daten)
    assert _ablauf(anlage, dienst) == "zurueckgerollt"
    assert not (w / "app" / "vorvorig.txt").exists() and not (w / "windows" / "vorvorig.txt").exists()
    nachher = {k: v for k, v in _baumstempel(w).items() if not k.startswith((ut.NEU, ut.ARBEIT))}
    assert nachher == vorher, "app und windows unverändert"
    s = ut.json_lesen(ut.status_pfad(daten))
    assert "nichts zurückgenommen, nichts verändert" in s["text"] and ALT_V in s["text"], s


@pytest.mark.parametrize("gesperrt_teil", ["app", "windows"])
def test_rueckfall_scheitert_ehrlich_und_wird_nachgeholt(anlage, schluessel, tmp_path, monkeypatch, gesperrt_teil):
    """Labtest 0.2.53 l4b: neue Fassung startet nicht UND app\\main.py ist während der Rücknahme
    gesperrt. Bisher verwarf ablauf() das Ergebnis und meldete „läuft wieder“ — bei neuer VERSION
    und ohne Listener. Jetzt: Status FEHLER mit Hinweis, Merker für die Rücknahme; beim nächsten
    Versuch (Dienststart) gelingt sie."""
    w, daten = anlage["wurzel"], anlage["daten"]
    dist = _release(tmp_path, schluessel)
    _auftrag_direkt(anlage, dist)
    dienst = _Dienst(w, daten, defekt={NEU_V})
    # „windows“: app\\ ist beim ersten Versuch schon zurück — der zweite muss es überspringen.
    gesperrt = _sperre(monkeypatch, w / gesperrt_teil)
    gesperrt["an"] = False

    echt_zurueck = ut.phase_zurueck

    def zurueck_mit_sperre(wurzel, lauf="", alt_entfernen=True):
        gesperrt["an"] = True
        try:
            return echt_zurueck(wurzel, lauf, alt_entfernen)
        finally:
            gesperrt["an"] = False
    monkeypatch.setattr(ut, "phase_zurueck", zurueck_mit_sperre)
    assert _ablauf(anlage, dienst) == "fehler"
    s = ut.json_lesen(ut.status_pfad(daten))
    befehl = "install.ps1" if os.name == "nt" else "systemctl restart exo-smtp-relay"
    assert s["zustand"] == "fehler" and "Rücknahme UNVOLLSTÄNDIG" in s["text"] and befehl in s["text"], s
    assert "PermissionError" in s["text"] or "zugreifen" in s["text"], s
    assert f"Fassung {NEU_V}" in s["text"], s
    assert "läuft wieder" not in s["text"]
    assert ut.ruecknahme_offen_pfad(daten).exists()

    monkeypatch.setattr(ut, "phase_zurueck", echt_zurueck)          # Sperre gelöst
    assert ut.ruecknahme_nachholen(w, daten) is True
    assert ut.aktuelle_version(w) == ALT_V
    s = ut.json_lesen(ut.status_pfad(daten))
    assert s["zustand"] == "zurueckgerollt" and "Rücknahme nachgeholt" in s["text"], s
    assert not ut.ruecknahme_offen_pfad(daten).exists()
    assert not [p for p in w.iterdir() if ".relay-verworfen" in p.name]


def test_offene_ruecknahme_vor_neuem_auftrag(anlage, schluessel, tmp_path):
    """Liegt eine offene Rücknahme vor und gelingt sie wieder nicht, wird kein neuer Tausch
    begonnen."""
    w, daten = anlage["wurzel"], anlage["daten"]
    ut.json_schreiben(ut.ruecknahme_offen_pfad(daten), {"lauf": "gibt-es-nicht", "von": ALT_V, "nach": NEU_V})
    ut.json_schreiben(w / ut.ALT / ut.MARKE, {"verschoben": [], "neu": [], "lauf": "gibt-es-nicht"})
    (w / ut.ALT / "app").mkdir()
    dist = _release(tmp_path, schluessel)
    _auftrag_direkt(anlage, dist)

    def phase(name, lauf=""):
        return (False, "gesperrt") if name == "zurueck" else (True, "")
    dienst = _Dienst(w, daten)
    assert _ablauf(anlage, dienst, phase) == "fehler"
    s = ut.json_lesen(ut.status_pfad(daten))
    assert "frühere Rücknahme ist noch offen" in s["text"], s
    assert ut.aktuelle_version(w) == ALT_V


def test_erfolg_raeumt_verworfene_reste_auf(anlage, schluessel, tmp_path):
    w = anlage["wurzel"]
    (w / "app.relay-verworfen").mkdir()
    (w / "app.relay-verworfen" / "x.py").write_text("x")
    dist = _release(tmp_path, schluessel)
    _auftrag_direkt(anlage, dist)
    assert _ablauf(anlage, _Dienst(w, anlage["daten"])) == "ok"
    assert not (w / "app.relay-verworfen").exists()



def test_pip_scheitert_in_der_ruecknahme_merker_bleibt(anlage, monkeypatch):
    """Geschwister-Suchlauf 0.2.54: scheiterte pip in der Rücknahme, war .update-alt samt Merker
    schon gelöscht — ein Nachholen meldete „nachgeholt“ ohne die Abhängigkeiten. Jetzt bleibt der
    Merker bis nach pip, der zweite Versuch installiert sie."""
    w, daten = anlage["wurzel"], anlage["daten"]
    alt = w / ut.ALT
    (alt / "app").mkdir(parents=True)
    (alt / "app" / "main.py").write_text("alt")
    (alt / "app" / "requirements.lock").write_text("alt==1\n")
    ut.json_schreiben(alt / ut.MARKE, {"verschoben": ["app"], "neu": [], "lauf": "L"})
    aufrufe = []

    def pip(wurzel, lock=None):
        aufrufe.append(1)
        if len(aufrufe) == 1:
            raise RuntimeError("pip: gesperrt")
        return "Abhängigkeiten angepasst"
    monkeypatch.setattr(ut, "phase_pip", pip)
    with pytest.raises(RuntimeError):
        ut.phase_ausfuehren("zurueck", w, daten, lauf="L")
    assert (alt / ut.MARKE).exists() and (w / "app" / "main.py").read_text() == "alt"
    assert "Abhängigkeiten angepasst" in ut.phase_ausfuehren("zurueck", w, daten, lauf="L")
    assert len(aufrufe) == 2 and not alt.exists()


# ── 0.2.55: Labtest 0.2.54 ───────────────────────────────────────────────────

def _offene_ruecknahme_anlegen(anlage, schluessel, tmp_path, monkeypatch):
    """Neue Fassung startet nicht, app\\ während der Rücknahme gesperrt → offene Rücknahme."""
    w, daten = anlage["wurzel"], anlage["daten"]
    dist = _release(tmp_path, schluessel)
    _auftrag_direkt(anlage, dist)
    gesperrt = _sperre(monkeypatch, w / "app")
    gesperrt["an"] = False
    echt = ut.phase_zurueck

    def zurueck_gesperrt(wurzel, lauf="", alt_entfernen=True):
        gesperrt["an"] = True
        try:
            return echt(wurzel, lauf, alt_entfernen)
        finally:
            gesperrt["an"] = False
    monkeypatch.setattr(ut, "phase_zurueck", zurueck_gesperrt)
    assert _ablauf(anlage, _Dienst(w, daten, defekt={NEU_V})) == "fehler"
    monkeypatch.setattr(ut, "phase_zurueck", echt)
    assert ut.ruecknahme_offen_pfad(daten).exists()


def test_knopf_holt_offene_ruecknahme_nach_statt_abzulehnen(anlage, schluessel, tmp_path, monkeypatch):
    """Labtest 0.2.54 ROT 1c: bei offener Rücknahme lehnte der Knopf mit 409 „Keine neuere Fassung
    bekannt“ ab. Jetzt beauftragt er das Nachholen; der Tauscher holt es nach."""
    import aktualisierung
    _offene_ruecknahme_anlegen(anlage, schluessel, tmp_path, monkeypatch)
    w, daten = anlage["wurzel"], anlage["daten"]
    assert aktualisierung.zustand()["ruecknahme_offen"] is True
    r = aktualisierung.einspielen("admin")
    assert r == {"ok": True, "nachholen": True}, r
    assert ut.json_lesen(ut.auftrag_pfad(daten))["nachholen"] is True
    dienst = _Dienst(w, daten)
    assert ut.ablauf(w, daten, dienst.stoppen, dienst.starten, dienst.laeuft) == "zurueckgerollt"
    assert ut.aktuelle_version(w) == ALT_V and not ut.ruecknahme_offen_pfad(daten).exists()
    assert "Rücknahme nachgeholt" in ut.json_lesen(ut.status_pfad(daten))["text"]


def test_dienststart_holt_nach_gemeinsamer_pfad(anlage, schluessel, tmp_path, monkeypatch):
    """ROT 1a/b: Unter systemd wurde die offene Rücknahme nie nachgeholt. Jetzt beim Dienststart —
    systemd über ExecStartPre (update_tausch.py nachholen), Windows über service.py — beide über
    beim_dienststart()."""
    _offene_ruecknahme_anlegen(anlage, schluessel, tmp_path, monkeypatch)
    w, daten = anlage["wurzel"], anlage["daten"]
    assert ut.main(["nachholen", "--wurzel", str(w), "--daten", str(daten)]) == 0
    assert ut.aktuelle_version(w) == ALT_V and not ut.ruecknahme_offen_pfad(daten).exists()
    assert ut.beim_dienststart(w, daten) is None                  # nichts mehr offen
    unit = (WURZEL / "linux" / "exo-smtp-relay.service").read_text(encoding="utf-8")
    assert re.search(r"^ExecStartPre=\+/opt/exo-smtp-relay/venv/bin/python /opt/exo-smtp-relay/app/update_tausch\.py nachholen ",
                     unit, re.M)
    assert "tausch.beim_dienststart(WURZEL, DATA, WRAPPER_FASSUNG)" in (WURZEL / "windows" / "service.py").read_text(encoding="utf-8")


def test_hinweis_plattformrichtig_und_kuendigt_ruecksprung_an(anlage):
    text = ut._offen_hinweis(anlage["wurzel"])
    if os.name == "nt":
        assert "Restart-Service ExoSmtpRelay" in text and "install.ps1" in text
    else:
        assert "systemctl restart exo-smtp-relay" in text and "install.ps1" not in text
    assert "auch wenn die neue inzwischen läuft" in text


@pytest.mark.parametrize("inhalt", [b"\xef\xbb\xbf{\"lauf\": \"", b"kaputt", b"[1, 2]"])
def test_unlesbare_offene_ruecknahme_ist_fehler_nicht_nichts_offen(anlage, inhalt):
    """ROT 3: {} galt als „nichts offen“, gemeldet wurde „nachgeholt“. Unlesbar ist FEHLER."""
    w, daten = anlage["wurzel"], anlage["daten"]
    ut.ruecknahme_offen_pfad(daten).parent.mkdir(parents=True, exist_ok=True)
    ut.ruecknahme_offen_pfad(daten).write_bytes(inhalt)
    assert ut.ruecknahme_nachholen(w, daten) is False
    s = ut.json_lesen(ut.status_pfad(daten))
    assert s["zustand"] == "fehler" and "NICHT nachgeholt" in s["text"], s
    assert ut.ruecknahme_offen_pfad(daten).exists()


def test_offene_ruecknahme_mit_bom_wird_gelesen(anlage):
    w, daten = anlage["wurzel"], anlage["daten"]
    ut.ruecknahme_offen_pfad(daten).parent.mkdir(parents=True, exist_ok=True)
    ut.ruecknahme_offen_pfad(daten).write_bytes(b"\xef\xbb\xbf" + json.dumps({"lauf": "x"}).encode())
    assert ut.json_lesen_streng(ut.ruecknahme_offen_pfad(daten)) == {"lauf": "x"}


def _status_gesperrt(monkeypatch, daten, mal):
    """os.replace auf status.json scheitert `mal` Mal (None = immer) — fremder Leser ohne
    FileShare.Delete (Labtest 0.2.54 ROT 2)."""
    echt = ut.os.replace
    zaehler = {"n": 0}

    def replace(a, b):
        if Path(b) == ut.status_pfad(daten) and (mal is None or zaehler["n"] < mal):
            zaehler["n"] += 1
            raise PermissionError(5, "Zugriff verweigert", str(b))
        return echt(a, b)
    monkeypatch.setattr(ut.os, "replace", replace)
    monkeypatch.setattr(ut, "SCHREIB_PAUSEN_S", (0.0, 0.0, 0.0))
    return zaehler


def test_status_schreiben_wiederholt(anlage, monkeypatch):
    daten = anlage["daten"]
    z = _status_gesperrt(monkeypatch, daten, 2)
    ut.status_setzen(daten, "laeuft", "x")
    assert z["n"] == 2 and ut.json_lesen(ut.status_pfad(daten))["text"] == "x"


def test_gesperrter_status_bricht_den_ablauf_nie_ab(anlage, schluessel, tmp_path, monkeypatch):
    """ROT 2: Eine dauerhaft gesperrte status.json beendete ablauf() — die neue Fassung lief ohne
    Überwachung, ohne Rücknahme. Jetzt geht der Ablauf weiter und rollt zurück."""
    w, daten = anlage["wurzel"], anlage["daten"]
    dist = _release(tmp_path, schluessel)
    _auftrag_direkt(anlage, dist)
    _status_gesperrt(monkeypatch, daten, None)
    dienst = _Dienst(w, daten, defekt={NEU_V})
    assert _ablauf(anlage, dienst) == "zurueckgerollt"
    assert ut.aktuelle_version(w) == ALT_V and dienst.starts[-1] == ALT_V


def test_tausch_merker_nicht_schreibbar_bricht_vor_dem_tausch_ab(anlage, schluessel, tmp_path, monkeypatch):
    """Kritische Merker (Tausch-Merker): wiederholen; scheitert es endgültig, Abbruch BEVOR etwas
    getauscht ist."""
    w, daten = anlage["wurzel"], anlage["daten"]
    dist = _release(tmp_path, schluessel)
    _auftrag_direkt(anlage, dist)
    vorher = _baumstempel(w)
    echt = ut.os.replace
    monkeypatch.setattr(ut, "SCHREIB_PAUSEN_S", (0.0,))

    def replace(a, b):
        if Path(b).name == ut.MARKE:
            raise PermissionError(5, "Zugriff verweigert", str(b))
        return echt(a, b)
    monkeypatch.setattr(ut.os, "replace", replace)
    assert _ablauf(anlage, _Dienst(w, daten)) == "zurueckgerollt"
    nachher = {k: v for k, v in _baumstempel(w).items() if not k.startswith((ut.NEU, ut.ARBEIT, ut.ALT))}
    assert nachher == vorher



def test_unlesbarer_status_ist_nicht_ruhe(anlage):
    import aktualisierung
    ut.status_pfad(anlage["daten"]).parent.mkdir(parents=True, exist_ok=True)
    ut.status_pfad(anlage["daten"]).write_bytes(b"\xef\xbb\xbf{kaputt")
    assert aktualisierung.status()["zustand"] == "fehler"
    ut.status_pfad(anlage["daten"]).write_bytes(b"\xef\xbb\xbf" + json.dumps({"zustand": "ok", "text": "x"}).encode())
    assert aktualisierung.status()["zustand"] == "ok"


# ── 0.2.55: systemd-Units ziehen mit ─────────────────────────────────────────

def _units_anlage(tmp_path, monkeypatch, schluessel):
    """/etc/systemd/system nachgebaut: Dienst-Unit in bekannter alter Fassung, .path vom Betreiber
    angepasst. Das neue Paket bringt beide in neuer Fassung."""
    import hashlib
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "exo-smtp-relay.service").write_bytes(b"[Service]\nExecStart=alt\n")
    (etc / "exo-smtp-relay-update.path").write_bytes(b"[Path]\n# vom Betreiber angepasst\n")
    (etc / "exo-smtp-relay.service.d").mkdir()
    (etc / "exo-smtp-relay.service.d" / "eigen.conf").write_text("[Service]\nNice=5\n")
    monkeypatch.setattr(ut, "SYSTEMD_ZIEL", etc)
    neu_laden = []
    monkeypatch.setattr(ut, "systemd_neu_laden", lambda: neu_laden.append(1))
    quelle = _baum(tmp_path / "q-units", NEU_V)
    (quelle / "linux" / "exo-smtp-relay.service").write_bytes(b"[Service]\nExecStartPre=+nachholen\nExecStart=neu\n")
    (quelle / "linux" / "exo-smtp-relay-update.path").write_bytes(b"[Path]\nneu\n")
    (quelle / "linux" / "einheiten-bekannt.json").write_text(json.dumps({
        "exo-smtp-relay.service": [hashlib.sha256(b"[Service]\nExecStart=alt\n").hexdigest()],
        "exo-smtp-relay-update.path": [hashlib.sha256(b"[Path]\nalt\n").hexdigest()]}))
    dist = tmp_path / "dist-units"
    pakete = up.bauen(quelle, dist, NEU_V)
    (dist / up.SUMMEN).write_text(up.summen_schreiben(pakete))
    (dist / up.SIGNATUR).write_text(base64.b64encode(schluessel.sign((dist / up.SUMMEN).read_bytes())).decode())
    return etc, neu_laden, dist


@pytest.mark.skipif(os.name == "nt", reason="systemd-Units nur unter Linux")
def test_units_werden_mitgezogen_angepasste_bleiben(anlage, schluessel, tmp_path, monkeypatch):
    etc, neu_laden, dist = _units_anlage(tmp_path, monkeypatch, schluessel)
    _auftrag_direkt(anlage, dist)
    assert _ablauf(anlage, _Dienst(anlage["wurzel"], anlage["daten"])) == "ok"
    assert (etc / "exo-smtp-relay.service").read_bytes() == b"[Service]\nExecStartPre=+nachholen\nExecStart=neu\n"
    assert (etc / "exo-smtp-relay-update.path").read_bytes() == b"[Path]\n# vom Betreiber angepasst\n"
    assert (etc / "exo-smtp-relay.service.d" / "eigen.conf").read_text() == "[Service]\nNice=5\n"
    assert neu_laden == [1]
    text = ut.json_lesen(ut.status_pfad(anlage["daten"]))["text"]
    assert "Units aktualisiert: exo-smtp-relay.service" in text
    assert "NICHT getauscht: exo-smtp-relay-update.path" in text


@pytest.mark.skipif(os.name == "nt", reason="systemd-Units nur unter Linux")
def test_ruecknahme_stellt_units_wieder_her(anlage, schluessel, tmp_path, monkeypatch):
    etc, neu_laden, dist = _units_anlage(tmp_path, monkeypatch, schluessel)
    _auftrag_direkt(anlage, dist)
    assert _ablauf(anlage, _Dienst(anlage["wurzel"], anlage["daten"], defekt={NEU_V})) == "zurueckgerollt"
    assert (etc / "exo-smtp-relay.service").read_bytes() == b"[Service]\nExecStart=alt\n"
    assert neu_laden == [1, 1], "daemon-reload beim Einspielen UND bei der Rücknahme"
    assert not [p for p in etc.iterdir() if p.name.endswith(".relay-neu")]


def test_ohne_systemd_keine_units(anlage, monkeypatch, tmp_path):
    monkeypatch.setattr(ut, "SYSTEMD_ZIEL", tmp_path / "gibt-es-nicht")
    assert ut.systemd_ziel() is None


def test_aktuelle_units_stehen_in_der_liste_bekannter_fassungen():
    """Jede Änderung an einer Unit braucht `tools/einheiten_bekannt.py schreiben` — sonst hielte der
    Updater die eigene neue Unit beim nächsten Mal für „vom Betreiber angepasst“."""
    sys.path.insert(0, str(WURZEL / "tools"))
    import einheiten_bekannt
    assert einheiten_bekannt.fehlende() == []
    assert set(einheiten_bekannt.EINHEITEN) == set(ut.EINHEITEN)


# ── 0.2.55: Windows — data\-Rechte und Dienst-Wrapper nach dem Knopf-Update ───

def _release_mit(tmp_path, k, dateien: dict, version=NEU_V):
    quelle = _baum(tmp_path / f"q-{version}-mit", version)
    for rel, inhalt in dateien.items():
        (quelle / rel).write_bytes(inhalt)
    dist = tmp_path / f"dist-{version}-mit"
    pakete = up.bauen(quelle, dist, version)
    (dist / up.SUMMEN).write_text(up.summen_schreiben(pakete))
    (dist / up.SIGNATUR).write_text(base64.b64encode(k.sign((dist / up.SUMMEN).read_bytes())).decode())
    return dist


def _windows(monkeypatch, ausgabe="OK: data\\ nur fuer SYSTEM und Administratoren lesbar", rc=0):
    monkeypatch.setattr(ut, "WINDOWS", True)
    aufrufe = {"ps": [], "neustart": 0}

    def ps(args):
        aufrufe["ps"].append(args)
        return rc, ausgabe
    monkeypatch.setattr(ut, "_powershell", ps)
    monkeypatch.setattr(ut, "_neustart_einplanen", lambda: aufrufe.__setitem__("neustart", aufrufe["neustart"] + 1))
    return aufrufe


def _wrapper(anlage, inhalt: bytes):
    """Installierter Wrapper; waechter.json meldet ihn als laufend (wie service.py beim Start)."""
    import hashlib
    (anlage["wurzel"] / "windows" / "service.py").write_bytes(inhalt)
    ut.json_schreiben(anlage["daten"] / "update" / "waechter.json",
                      {"protokoll": ut.PROTOKOLL, "fassung": hashlib.sha256(inhalt).hexdigest()[:12]})


def test_knopf_update_setzt_data_rechte_ueber_install_ps1(anlage, schluessel, tmp_path, monkeypatch):
    """(a) Wer nur per Knopf aktualisierte, bekam die data\\-Rechte (0.2.45) nie. Jetzt ruft der
    Tauscher install.ps1 -NurDatenRechte auf — dieselbe Funktion wie die Installation."""
    aufrufe = _windows(monkeypatch, "==> Setze Rechte\n    OK: entfernt: BUILTIN\\Backup Operators Allow Read\n"
                                    "    OK: data\\ nur fuer SYSTEM und Administratoren lesbar (1 fremde Eintraege entfernt)\n")
    dist = _release(tmp_path, schluessel)
    _auftrag_direkt(anlage, dist)
    assert _ablauf(anlage, _Dienst(anlage["wurzel"], anlage["daten"])) == "ok"
    args = aufrufe["ps"][0]
    assert args[:2] == ["-File", str(anlage["wurzel"] / "windows" / "install.ps1")] and "-NurDatenRechte" in args
    assert args[args.index("-InstallDir") + 1] == str(anlage["wurzel"])
    text = ut.json_lesen(ut.status_pfad(anlage["daten"]))["text"]
    assert "Rechte auf data\\: entfernt: BUILTIN\\Backup Operators Allow Read" in text, text


@pytest.mark.parametrize("rc, ausgabe, erwartet", [
    (1, "Bitte als Administrator ausfuehren.", "WARNUNG: Rechte auf data\\ nicht gesetzt (Exit 1)"),
    (0, "    WARNUNG: nicht zurueckgesetzt: x.json (Exit 5: Access is denied)", "WARNUNG: nicht zurueckgesetzt: x.json"),
])
def test_data_rechte_scheitern_bricht_nicht_ab(anlage, schluessel, tmp_path, monkeypatch, rc, ausgabe, erwartet):
    _windows(monkeypatch, ausgabe, rc)
    dist = _release(tmp_path, schluessel)
    _auftrag_direkt(anlage, dist)
    assert _ablauf(anlage, _Dienst(anlage["wurzel"], anlage["daten"])) == "ok"
    assert erwartet in ut.json_lesen(ut.status_pfad(anlage["daten"]))["text"]


def test_install_ps1_nur_daten_rechte_ist_derselbe_pfad():
    code = (WURZEL / "windows" / "install.ps1").read_text(encoding="utf-8-sig")
    m = re.search(r"if \(\$NurDatenRechte\) \{(.*?)\n\}", code, re.S)
    assert m and "Set-RelayDataAcl -Pfad (Join-Path $InstallDir \"data\")" in m.group(1) and "exit 0" in m.group(1)
    assert code.index("if ($NurDatenRechte)") < code.index("# -- Python finden oder installieren")
    assert ".PARAMETER NurDatenRechte" in code


def test_neuer_wrapper_neustart_eingeplant_und_bestaetigt(anlage, schluessel, tmp_path, monkeypatch):
    """(b) Den neuen windows\\service.py lud Windows erst beim nächsten Dienstneustart. Jetzt plant
    der Tauscher den Neustart (einmalige Aufgabe), der neue Wrapper bestätigt ihn."""
    import hashlib
    aufrufe = _windows(monkeypatch)
    _wrapper(anlage, b"# wrapper alt\n")
    dist = _release_mit(tmp_path, schluessel, {"windows/service.py": b"# wrapper neu\n"})
    _auftrag_direkt(anlage, dist)
    assert _ablauf(anlage, _Dienst(anlage["wurzel"], anlage["daten"])) == "ok"
    assert aufrufe["neustart"] == 1
    daten = anlage["daten"]
    neu = hashlib.sha256(b"# wrapper neu\n").hexdigest()[:12]
    assert "Neustart des Windows-Dienstes eingeplant" in ut.json_lesen(ut.status_pfad(daten))["text"]
    assert ut.json_lesen(daten / "update" / ut.WRAPPER_NEUSTART)["erwartet"] == neu
    ut.beim_dienststart(anlage["wurzel"], daten, neu)                       # der neue Wrapper startet
    assert f"Dienst-Wrapper neu gestartet (Fassung {neu})" in ut.json_lesen(ut.status_pfad(daten))["text"]
    assert not (daten / "update" / ut.WRAPPER_NEUSTART).exists()


def test_falscher_wrapper_nach_neustart_ist_warnung(anlage, monkeypatch):
    daten = anlage["daten"]
    ut.json_schreiben(daten / "update" / ut.WRAPPER_NEUSTART, {"erwartet": "aaaaaaaaaaaa"})
    ut.status_setzen(daten, "ok", "0.2.41 läuft")
    ut.wrapper_gestartet(daten, "bbbbbbbbbbbb")
    assert "WARNUNG: Dienst-Wrapper läuft in Fassung bbbbbbbbbbbb" in ut.json_lesen(ut.status_pfad(daten))["text"]


def test_gleicher_wrapper_kein_neustart(anlage, schluessel, tmp_path, monkeypatch):
    aufrufe = _windows(monkeypatch)
    _wrapper(anlage, b"# wrapper\n")
    dist = _release_mit(tmp_path, schluessel, {"windows/service.py": b"# wrapper\n"})
    _auftrag_direkt(anlage, dist)
    assert _ablauf(anlage, _Dienst(anlage["wurzel"], anlage["daten"])) == "ok"
    assert aufrufe["neustart"] == 0


@pytest.mark.parametrize("laufend, neustarts", [(b"# wrapper alt\n", 0), (b"# wrapper neu\n", 1)])
def test_ruecknahme_plant_neustart_wenn_der_laufende_wrapper_abweicht(anlage, schluessel, tmp_path, monkeypatch,
                                                                     laufend, neustarts):
    """Rücknahme: Liegt danach der alte Wrapper wieder da und läuft auch der alte → nichts; läuft
    schon der neue (Neustart war erfolgt) → Neustart einplanen."""
    import hashlib
    aufrufe = _windows(monkeypatch)
    (anlage["wurzel"] / "windows" / "service.py").write_bytes(b"# wrapper alt\n")
    ut.json_schreiben(anlage["daten"] / "update" / "waechter.json",
                      {"protokoll": ut.PROTOKOLL, "fassung": hashlib.sha256(laufend).hexdigest()[:12]})
    dist = _release_mit(tmp_path, schluessel, {"windows/service.py": b"# wrapper neu\n"})
    _auftrag_direkt(anlage, dist)
    assert _ablauf(anlage, _Dienst(anlage["wurzel"], anlage["daten"], defekt={NEU_V})) == "zurueckgerollt"
    assert (anlage["wurzel"] / "windows" / "service.py").read_bytes() == b"# wrapper alt\n"
    assert aufrufe["neustart"] == neustarts


def test_neustart_nicht_einplanbar_nennt_befehl(anlage, schluessel, tmp_path, monkeypatch):
    _windows(monkeypatch)

    def scheitert():
        raise RuntimeError("schtasks /Create: Zugriff verweigert")
    monkeypatch.setattr(ut, "_neustart_einplanen", scheitert)
    _wrapper(anlage, b"# wrapper alt\n")
    dist = _release_mit(tmp_path, schluessel, {"windows/service.py": b"# wrapper neu\n"})
    _auftrag_direkt(anlage, dist)
    assert _ablauf(anlage, _Dienst(anlage["wurzel"], anlage["daten"])) == "ok"
    text = ut.json_lesen(ut.status_pfad(anlage["daten"]))["text"]
    assert "Neustart NICHT eingeplant" in text and "Restart-Service ExoSmtpRelay" in text


def test_wrapper_meldet_seine_fassung_beim_start():
    code = (WURZEL / "windows" / "service.py").read_text(encoding="utf-8")
    assert '"fassung": WRAPPER_FASSUNG' in code
    assert "tausch.beim_dienststart(WURZEL, DATA, WRAPPER_FASSUNG)" in code
    assert code.index("self._waechter_melden()\n        self._nachholen()") > 0
