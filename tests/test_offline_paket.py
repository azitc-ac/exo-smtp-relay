"""Lieferkette und Offline-Paket (0.2.47).

* install.ps1 lädt nur über Get-RelayDownload und führt nichts aus, dessen SHA256 und
  Authenticode-Signierer nicht fest eingetragen sind (Python-Installer,
  ExchangeOnlineManagement-nupkg). Offline verweigert der Download.
* exo-smtp-relay-<v>-windows-offline.zip: App + offline/ (inhalt.json, wheels, python,
  module); steht in SHA256SUMS wie die anderen Pakete.
* Eine offline installierte Instanz lädt beim Update das Offline-Paket; update_tausch
  installiert dessen Wheels ohne Index. install.ps1 und der Updater bauen DIESELBEN
  pip-Argumente.

Die echte Authenticode-Prüfung und die Installation ohne Netz macht der CI-Job
„Offline-Paket“ (tools/offline_probe.ps1) auf Windows.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from conftest import WURZEL
import update_paket as up
import update_tausch as ut
from test_aktualisierung import ALT_V, NEU_V, _Dienst, _api, _baum, anlage, schluessel  # noqa: F401

sys.path.insert(0, str(WURZEL / "tools"))
import offline_paket  # noqa: E402

INSTALL = WURZEL / "windows" / "install.ps1"


def _ps():
    return shutil.which("pwsh") or shutil.which("powershell")


def _code() -> str:
    return INSTALL.read_text(encoding="utf-8-sig")


def _ohne_kommentare(text: str) -> str:
    return "\n".join(z for z in text.splitlines() if not z.lstrip().startswith("#"))


def _funktion(name: str) -> str:
    m = re.search(rf"^function {re.escape(name)}\b.*?^\}}", _code(), re.S | re.M)
    assert m, name
    return m.group(0)


def _tabelle_ps(name: str) -> str:
    m = re.search(rf"^\${name} = @\{{.*?^\}}", _code(), re.S | re.M)
    assert m, name
    return m.group(0)


def _stufe(ziel: Path) -> Path:
    for teil, datei, inhalt in (("wheels", "x-1-py3-none-any.whl", b"rad"), ("python", "py.exe", b"MZ"),
                                ("module", "m.nupkg", b"PK")):
        (ziel / teil).mkdir(parents=True)
        (ziel / teil / datei).write_bytes(inhalt)
    (ziel / "inhalt.json").write_text('{"format": 1}')
    return ziel


# ── Feste Quellen in install.ps1 ─────────────────────────────────────────────

def test_feste_quellen_mit_pruefsumme():
    py, modul = offline_paket.tabelle("RelayPythonInstaller"), offline_paket.tabelle("RelayExoModul")
    assert py["Url"] == f"https://www.python.org/ftp/python/{py['Version']}/{py['Datei']}"
    assert py["Signierer"] == "Python Software Foundation"
    assert py["Datei"].endswith("-amd64.exe")
    assert modul["Url"].startswith("https://www.powershellgallery.com/api/v2/package/ExchangeOnlineManagement/")
    assert modul["Version"] == "3.10.1" and modul["Signierer"] == "Microsoft Corporation"
    # Dieselbe Fassung wie im Dockerfile (und im Gateway).
    assert "-RequiredVersion 3.10.1" in (WURZEL / "Dockerfile").read_text(encoding="utf-8")


def test_nur_ein_downloadweg_und_kein_install_module():
    code = _ohne_kommentare(_code())
    assert code.count("Invoke-WebRequest") == 1 and "Invoke-WebRequest" in _funktion("Get-RelayDownload")
    for verboten in ("Install-Module", "Install-PackageProvider", "DownloadFile", "Start-BitsTransfer",
                     "Invoke-RestMethod", "--upgrade pip"):
        assert verboten not in code, verboten
    # Jeder Download landet in einer Funktion, die vor Gebrauch Prüfsumme (und Signatur) prüft.
    aufrufe = re.findall(r"Get-RelayDownload \$(\w+)\.Url \$(\w+)", code)
    assert sorted(a for a, _ in aufrufe) == ["em", "pq"], aufrufe
    assert "Test-RelayDatei $Installer $RelayPythonInstaller" in _funktion("Install-RelayPython")
    assert "Test-RelayDatei $Nupkg $em -OhneSignatur" in _funktion("Install-RelayExoModul")
    assert "Test-RelaySignatur $s.FullName $em.Signierer" in _funktion("Install-RelayExoModul")
    assert re.search(r"Install-RelayPython \$installer", code)
    assert re.search(r"Install-RelayExoModul \$nupkg", code)


_SIG_MOCK = r'''
$global:SigStatus = "Valid"; $global:SigSubjekt = "CN=Python Software Foundation, O=Python Software Foundation, L=Beaverton, S=Oregon, C=US"
function Get-AuthenticodeSignature { param($FilePath)
    [pscustomobject]@{ Status = $global:SigStatus; StatusMessage = "Nachbau"; SignerCertificate = [pscustomobject]@{ Subject = $global:SigSubjekt } } }
'''


def _signatur_lauf(tmp_path, funktionen: str) -> dict:
    datei = tmp_path / "python-3.12.10-amd64.exe"
    datei.write_bytes(b"MZ-Nachbau")
    import hashlib
    h = hashlib.sha256(datei.read_bytes()).hexdigest()
    q = f'@{{ Datei = "{datei.name}"; Sha256 = "{h}"; Signierer = "Python Software Foundation" }}'
    falsch = f'@{{ Datei = "{datei.name}"; Sha256 = "{"0" * 64}"; Signierer = "Python Software Foundation" }}'
    skript = tmp_path / "sig.ps1"
    skript.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n" + funktionen + _SIG_MOCK +
        "function Probe([scriptblock]$b) { try { & $b; 'ok' } catch { \"abgelehnt: $($_.Exception.Message)\" } }\n"
        "$e = [ordered]@{}\n"
        f"$e.gut = Probe {{ Test-RelayDatei '{datei}' {q} }}\n"
        f"$e.hash = Probe {{ Test-RelayDatei '{datei}' {falsch} }}\n"
        f"$global:SigStatus = 'NotSigned'; $e.unsigniert = Probe {{ Test-RelayDatei '{datei}' {q} }}\n"
        "$global:SigStatus = 'Valid'; $global:SigSubjekt = 'CN=Irgendwer, O=Irgendwer'\n"
        f"$e.fremd = Probe {{ Test-RelayDatei '{datei}' {q} }}\n"
        f"$e.ohne = Probe {{ Test-RelayDatei '{datei}' {q} -OhneSignatur }}\n"
        "$global:SigSubjekt = 'CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond'\n"
        f"$e.microsoft = Probe {{ Test-RelaySignatur '{datei}' 'Microsoft Corporation' }}\n"
        f"$e.fehlt = Probe {{ Test-RelayDatei '{tmp_path / 'nicht.exe'}' {q} }}\n"
        "ConvertTo-Json -Compress -InputObject $e\n", encoding="utf-8-sig")
    r = subprocess.run([_ps(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(skript)],
                       capture_output=True, text=True, timeout=120,
                       env={k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"})
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_pruefsumme_und_signatur_entscheiden(tmp_path):
    e = _signatur_lauf(tmp_path, _funktion("Test-RelaySignatur") + "\n" + _funktion("Test-RelayDatei"))
    assert e["gut"] == "ok" and e["ohne"] == "ok" and e["microsoft"] == "ok", e
    assert "Pruefsumme stimmt nicht" in e["hash"], e
    assert "nicht gueltig (NotSigned" in e["unsigniert"], e
    assert "erwartet 'Python Software Foundation'" in e["fremd"], e
    assert "fehlt" in e["fehlt"], e


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_gegenprobe_ohne_signaturpruefung(tmp_path):
    """Trennscharf: Test-RelayDatei ohne Signaturprüfung lässt unsignierte und fremde Dateien durch."""
    ohne = _funktion("Test-RelayDatei").replace("if (-not $OhneSignatur) { Test-RelaySignatur $Pfad $Quelle.Signierer }", "")
    assert ohne != _funktion("Test-RelayDatei")
    e = _signatur_lauf(tmp_path, _funktion("Test-RelaySignatur") + "\n" + ohne)
    assert e["unsigniert"] == "ok" and e["fremd"] == "ok"


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_offline_verweigert_download(tmp_path):
    skript = tmp_path / "dl.ps1"
    skript.write_text("Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
                      + _funktion("Get-RelayDownload") +
                      "\nfunction Invoke-WebRequest { 'GELADEN' }\n$script:RelayOffline = $true\n"
                      "try { Get-RelayDownload 'https://example.invalid/x' 'x'; 'GELADEN' } catch { \"$($_.Exception.Message)\" }\n",
                      encoding="utf-8-sig")
    r = subprocess.run([_ps(), "-NoProfile", "-NonInteractive", "-File", str(skript)], capture_output=True, text=True,
                       timeout=60, env={k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"})
    assert r.stdout.strip().splitlines()[-1].startswith("Offline-Installation: kein Download"), r.stdout + r.stderr


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
@pytest.mark.parametrize("offline", [False, True])
def test_pip_argumente_wie_im_updater(tmp_path, offline):
    """install.ps1 (Get-RelayPipArgumente) und update_tausch.pip_argumente: EIN Weg."""
    if offline:
        (tmp_path / "offline" / "wheels").mkdir(parents=True)
        (tmp_path / "offline" / "inhalt.json").write_text("{}")
    lock = tmp_path / "app" / "requirements.lock"
    skript = tmp_path / "pip.ps1"
    skript.write_text("Set-StrictMode -Version Latest\n" + _funktion("Get-RelayPipArgumente")
                      + f"\nConvertTo-Json -Compress -InputObject @(Get-RelayPipArgumente -InstallDir '{tmp_path}' -Lock '{lock}')\n",
                      encoding="utf-8-sig")
    r = subprocess.run([_ps(), "-NoProfile", "-NonInteractive", "-File", str(skript)], capture_output=True, text=True,
                       timeout=60, env={k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"})
    ps = [a.replace("\\", "/") for a in json.loads(r.stdout.strip().splitlines()[-1])]
    py = [a.replace("\\", "/") for a in ut.pip_argumente(tmp_path, lock)]
    assert ps == py
    assert ("--no-index" in py) is offline and "--require-hashes" in py


def test_install_nutzt_gemeinsame_pip_argumente():
    code = _ohne_kommentare(_code())
    assert code.count("Get-RelayPipArgumente -InstallDir $InstallDir -Lock $lock") == 2   # Installation + Rücknahme
    assert "-m pip install" not in code                 # kein zweiter, eigener pip-Aufruf


# ── Paket bauen und entpacken ────────────────────────────────────────────────

def test_offline_paket_bauen_und_fuer_den_updater_entpacken(tmp_path):
    quelle = _baum(tmp_path / "q", NEU_V)
    pakete = up.bauen(quelle, tmp_path / "dist", NEU_V, _stufe(tmp_path / "stufe"))
    namen = [p.name for p in pakete]
    assert namen == [up.paket_name(NEU_V, "windows"), up.paket_name(NEU_V, "windows-offline"),
                     up.paket_name(NEU_V, "linux")]
    off = pakete[1]
    assert up.paket_name_gueltig(off.name)
    inhalt = zipfile.ZipFile(off).namelist()
    kopf = f"exo-smtp-relay-{NEU_V}/"
    for n in ("app/main.py", "offline/inhalt.json", "offline/wheels/x-1-py3-none-any.whl",
              "offline/python/py.exe", "offline/module/m.nupkg"):
        assert kopf + n in inhalt, n
    assert up.summen_lesen(up.summen_schreiben(pakete)).keys() == set(namen)
    ziel = tmp_path / "neu"
    up.entpacken(off, ziel, NEU_V)
    assert (ziel / "offline" / "wheels" / "x-1-py3-none-any.whl").is_file()
    assert not (ziel / "offline" / "python").exists() and not (ziel / "offline" / "module").exists()
    # Ohne Stufe: kein Offline-Paket, und das normale enthält kein offline/.
    normal = up.bauen(quelle, tmp_path / "dist2", NEU_V)
    assert len(normal) == 2 and not [n for n in zipfile.ZipFile(normal[0]).namelist() if "/offline/" in n]


def test_offline_stufe_unvollstaendig(tmp_path):
    stufe = _stufe(tmp_path / "stufe")
    shutil.rmtree(stufe / "module")
    with pytest.raises(up.PaketFehler, match="module"):
        up.bauen(_baum(tmp_path / "q", NEU_V), tmp_path / "dist", NEU_V, stufe)


# ── Updater: offline installiert → Offline-Paket, Wheels ohne Index ──────────

def test_offline_instanz_aktualisiert_mit_offline_paket(anlage, schluessel, tmp_path, monkeypatch):
    import aktualisierung
    w, daten = anlage["wurzel"], anlage["daten"]
    (w / "offline" / "wheels").mkdir(parents=True)
    (w / "offline" / "wheels" / "alt-1-py3-none-any.whl").write_bytes(b"alt")
    (w / "offline" / "inhalt.json").write_text('{"relay": "alt"}')
    monkeypatch.setattr(aktualisierung, "WURZEL", w)
    assert aktualisierung.paket_art("windows") == "windows-offline"
    assert aktualisierung.zustand()["offline"] is True

    dist = tmp_path / "dist"
    pakete = up.bauen(_baum(tmp_path / "q", NEU_V, lock="x==2\n"), dist, NEU_V, _stufe(tmp_path / "stufe"))
    (dist / up.SUMMEN).write_text(up.summen_schreiben(pakete))
    (dist / up.SIGNATUR).write_text(base64.b64encode(schluessel.sign((dist / up.SUMMEN).read_bytes())).decode())
    anlage["veroeffentlichen"](dist, [_api(dist)])
    assert aktualisierung.pruefen()["verfuegbar"]
    assert aktualisierung.einspielen("admin") == {"ok": True, "version": NEU_V}
    auftrag = ut.json_lesen(ut.auftrag_pfad(daten))
    assert auftrag["paket"] == up.paket_name(NEU_V, "windows-offline")

    aufrufe = []

    class _R:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(ut.subprocess, "run", lambda args, **kw: aufrufe.append(args) or _R())
    py = ut.venv_python(w)
    py.parent.mkdir(parents=True)
    py.write_text("")
    dienst = _Dienst(w, daten)
    uhr = [0.0]
    ende = ut.ablauf(w, daten, dienst.stoppen, dienst.starten, dienst.laeuft,
                     phase=ut.phasen_im_prozess(w, daten, NEU_V, auftrag["paket"]),
                     warte_s=30, stabil_s=5, schlafen=lambda s: uhr.__setitem__(0, uhr[0] + s), uhr=lambda: uhr[0])
    assert ende == "ok", ut.json_lesen(ut.status_pfad(daten))
    assert (w / "offline" / "wheels" / "x-1-py3-none-any.whl").is_file(), "neue Wheels getauscht"
    assert not (w / "offline" / "wheels" / "alt-1-py3-none-any.whl").exists()
    assert not (w / "offline" / "python").exists()
    pip = aufrufe[-1]
    assert "--no-index" in pip and pip[pip.index("--find-links") + 1] == str(w / "offline" / "wheels")


def test_online_instanz_laedt_normales_paket(anlage, monkeypatch):
    import aktualisierung
    monkeypatch.setattr(aktualisierung, "WURZEL", anlage["wurzel"])
    assert aktualisierung.paket_art("windows") == "windows"
    assert aktualisierung.paket_art("systemd") == "linux"
    assert aktualisierung.zustand()["offline"] is False


def test_oberflaeche_zeigt_installationsart():
    html = (WURZEL / "app" / "webui" / "templates" / "aktualisierung.html").read_text(encoding="utf-8")
    assert 'id="installationsart"' in html and "z.offline" in html and "z.paket" in html


# ── Workflows ────────────────────────────────────────────────────────────────

def test_release_baut_prueft_und_signiert_offline_paket():
    yml = (WURZEL / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    pakete = yml.split("\n  image:", 1)[0]
    assert "tools/offline_paket.py vorbereiten" in pakete
    assert pakete.index("offline_paket.py vorbereiten") < pakete.index("release.py bauen dist") \
        < pakete.index("release.py signieren")
    assert "release.py bauen dist offline-stufe" in pakete
    assert "offline_probe.ps1" in yml and "needs: [pakete, manifest, offline]" in yml


def test_ci_installiert_offline_paket_ohne_netz():
    yml = (WURZEL / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    teil = yml.split("\n  offline:", 1)[1].split("\n  build:", 1)[0]
    assert "runs-on: windows-latest" in teil and "offline_probe.ps1" in teil
    probe = (WURZEL / "tools" / "offline_probe.ps1").read_text(encoding="utf-8-sig")
    for muss in ("PIP_NO_INDEX", "New-NetFirewallRule", "-Direction Outbound", "Offline-Installation",
                 "phase_pip", "Gegenprobe: veraenderter Installer abgelehnt"):
        assert muss in probe, muss
