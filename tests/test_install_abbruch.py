"""Labtest 0.2.45: Ein Abbruch von install.ps1 NACH dem Dienststopp liess ein gestopptes Relay
mit schon neuer VERSION zurück (halbes Upgrade). Und venv, pip und service.py meldeten nach
einem Fehler trotzdem „OK“, weil niemand den Exit-Code ansah.

Jetzt: Invoke-RelayNativ prüft den Exit-Code (stderr unter PS 5.1 ohne Abbruch), die vorige
Fassung wandert vor dem Kopieren nach .install-alt\\ und kommt bei jedem Fehler zurück, der
Dienst startet wieder, wenn er vorher lief. Der echte Durchlauf auf Windows steht im CI-Job
„Installer echt“ (tests/installer_echt.ps1).
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from conftest import WURZEL

INSTALL = WURZEL / "windows" / "install.ps1"


def _ps() -> str | None:
    # Windows PowerShell 5.1 zuerst: Dort (nicht in pwsh 7) wird stderr bei 2>&1 unter
    # ErrorActionPreference=Stop zum beendenden Fehler — genau der Labtest-Fall.
    return shutil.which("powershell") or shutil.which("pwsh")


def _code() -> str:
    return INSTALL.read_text(encoding="utf-8-sig")


def _funktion(name: str) -> str:
    m = re.search(rf"^function {re.escape(name)}\b.*?^\}}", _code(), re.S | re.M)
    assert m, f"{name} fehlt in install.ps1"
    return m.group(0)


def _teile() -> str:
    m = re.search(r"^\$RelayTeile = @\(.*?\)$", _code(), re.M)
    assert m
    return m.group(0)


def _lauf(tmp_path, rumpf: str, env: dict | None = None) -> dict:
    skript = tmp_path / "t.ps1"
    skript.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        "$global:M = New-Object System.Collections.ArrayList\n"
        "function Write-Ok([string]$m) { [void]$global:M.Add(\"OK: $m\") }\n"
        "function Write-Warn([string]$m) { [void]$global:M.Add(\"WARN: $m\") }\n"
        + _teile() + "\n"
        + "\n".join(_funktion(n) for n in ("Invoke-RelayNativ", "Backup-RelayFassung",
                                             "Restore-RelayFassung", "Restore-RelayNachAbbruch"))
        + "\n" + rumpf, encoding="utf-8-sig")
    umgebung = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    r = subprocess.run([_ps(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(skript)],
                       capture_output=True, text=True, timeout=120, env=umgebung)
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_invoke_relaynativ_prueft_exitcode_und_ueberlebt_stderr(tmp_path):
    """ECHTER Unterprozess mit stderr und Exit 3 — unter PS 5.1 (Windows-CI) wäre die stderr-Zeile
    ohne die lokale EAP-Absicherung ein beendender Fehler OHNE Exit-Code."""
    ps = _ps().replace("\\", "\\\\")
    e = _lauf(tmp_path, f'''
$ps = "{ps}"
$gut = @(Invoke-RelayNativ "gut" {{ & $ps -NoProfile -Command "[Console]::Error.WriteLine('nur Hinweis'); 'zeile1'; 'zeile2'; exit 0" }})
try {{ Invoke-RelayNativ "schlecht" {{ & $ps -NoProfile -Command "'vorher'; [Console]::Error.WriteLine('kaputt'); exit 3" }}; $schlecht = "kein Fehler" }}
catch {{ $schlecht = "$($_.Exception.Message)" }}
try {{ Invoke-RelayNativ "fehlt" {{ & "C:\\gibt\\es\\nicht.exe" }}; $fehlt = "kein Fehler" }} catch {{ $fehlt = "$($_.Exception.Message)" }}
ConvertTo-Json -Compress -InputObject ([ordered]@{{ Gut = $gut; Schlecht = $schlecht; Fehlt = $fehlt }})
''')
    assert e["Gut"] == ["zeile1", "zeile2"], e                     # nur stdout, stderr nicht im Ergebnis
    assert e["Schlecht"].startswith("schlecht fehlgeschlagen (Exit 3)") and "kaputt" in e["Schlecht"], e
    assert e["Fehlt"] != "kein Fehler" and "nicht.exe" in e["Fehlt"], e   # nie stilles OK


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_sicherung_und_ruecknahme(tmp_path):
    ziel, sich = tmp_path / "inst", tmp_path / "inst" / ".install-alt"
    (ziel / "app").mkdir(parents=True)
    (ziel / "app" / "main.py").write_text("alt")
    (ziel / "VERSION").write_text("0.2.44")
    (ziel / "data").mkdir()
    (ziel / "data" / "settings.json").write_text("{}")
    e = _lauf(tmp_path, f'''
$g = @(Backup-RelayFassung -Ziel "{ziel}" -Sicherung "{sich}" -Teile $RelayTeile)
$nachSicherung = @(Get-ChildItem "{ziel}" -Name -Force | Sort-Object)
New-Item -ItemType Directory "{ziel}/app" | Out-Null
Set-Content "{ziel}/app/main.py" "neu"
Set-Content "{ziel}/VERSION" "0.2.46"
Set-Content "{ziel}/README.md" "neu"
Restore-RelayFassung -Ziel "{ziel}" -Sicherung "{sich}" -Teile $g
ConvertTo-Json -Compress -InputObject ([ordered]@{{ Gesichert = $g; NachSicherung = $nachSicherung;
    Main = (Get-Content "{ziel}/app/main.py" -Raw).Trim(); Version = (Get-Content "{ziel}/VERSION" -Raw).Trim();
    SicherungDa = (Test-Path "{sich}") }})
''')
    assert e["Gesichert"] == ["app", "VERSION"], e
    assert e["NachSicherung"] == [".install-alt", "data"], e      # data\ bleibt immer an Ort und Stelle
    assert e["Main"] == "alt" and e["Version"] == "0.2.44", e
    assert e["SicherungDa"] is False
    assert (ziel / "data" / "settings.json").read_text() == "{}"


_DIENST_MOCK = '''
$global:Dienst = New-Object System.Collections.ArrayList
function Stop-Service { param($Name, [switch]$Force) [void]$global:Dienst.Add("stop") }
function Start-Service { param($Name) [void]$global:Dienst.Add("start") }
'''


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
@pytest.mark.parametrize("lief", [True, False])
def test_abbruch_holt_vorige_fassung_und_startet_den_dienst(tmp_path, lief):
    ziel = tmp_path / "inst"
    (ziel / "app").mkdir(parents=True)
    (ziel / "VERSION").write_text("0.2.44")
    e = _lauf(tmp_path, _DIENST_MOCK + f'''
$InstallDir = "{ziel}"; $sicherung = "{ziel}/.install-alt"; $dataDir = "{ziel}/data"
$venvPython = "{ziel}/venv/gibtsnicht"; $pipGelaufen = $true; $istUpgrade = $true
$dienstLief = ${str(lief).lower()}; $altVersion = "0.2.44"
$gesichert = @(Backup-RelayFassung -Ziel $InstallDir -Sicherung $sicherung -Teile $RelayTeile)
New-Item -ItemType Directory "{ziel}/app" | Out-Null
Set-Content "{ziel}/VERSION" "0.2.46"
Restore-RelayNachAbbruch
ConvertTo-Json -Compress -InputObject ([ordered]@{{ Version = (Get-Content "{ziel}/VERSION" -Raw).Trim();
    Dienst = @($global:Dienst); Meldungen = @($global:M) }})
''')
    assert e["Version"] == "0.2.44", e
    assert e["Dienst"] == (["stop", "start"] if lief else ["stop"]), e
    assert any("Vorige Fassung 0.2.44 zurueckgeholt" in m for m in e["Meldungen"]), e
    if lief:
        assert e["Meldungen"][-1] == "WARN: Dienst laeuft wieder (Fassung 0.2.44)", e


def _hauptteil(code: str) -> str:
    return code[code.index("# -- Dienst anhalten, falls vorhanden"):code.index("# Kennwort-Hinweis")]


def _verstoesse(code: str) -> list[str]:
    """Alles zwischen Stop-Service und Start-Service liegt im try; der catch holt die vorige
    Fassung zurück; jedes native Programm läuft über Invoke-RelayNativ."""
    h = _hauptteil(code)
    aus = []
    stopp = h.find('Stop-Service -Name "ExoSmtpRelay" -Force')
    versuch, fang = h.find("\ntry {\n"), h.find("\n} catch {\n")
    if not (0 <= stopp < versuch < h.find("Backup-RelayFassung") < h.find("Copy-Item") < fang):
        aus.append("kopieren_im_try")
    if not (versuch < h.find('Start-Service -Name "ExoSmtpRelay"') < fang) or versuch < 0:
        aus.append("start_im_try")
    if fang < 0 or "Restore-RelayNachAbbruch" not in h[fang:]:
        aus.append("ruecknahme_im_catch")
    for z in h.splitlines():
        s = z.strip()
        if not s.startswith("#") and re.search(r"&\s*\$(venvPython|PythonExe)\b", s) and "Invoke-RelayNativ" not in s:
            aus.append("exitcode_ungeprueft")
            break
    return aus


def test_ablauf_nach_dem_stopp_ist_abgesichert():
    assert _verstoesse(_code()) == []
    for was in ("venv anlegen", "pip install (requirements.lock)", "service.py update", "service.py install"):
        assert f'Invoke-RelayNativ "{was}"' in _hauptteil(_code()), was


def test_gegenprobe_ablauf_0245_faellt_durch():
    """Trennscharf: derselbe Prüfer auf den Hauptteil von 0.2.45 meldet alle vier Verstösse."""
    alt = subprocess.run(["git", "-C", str(WURZEL), "show", "4d78c34:windows/install.ps1"],
                         capture_output=True, text=True, encoding="utf-8")
    if alt.returncode != 0:
        pytest.skip("Stand 0.2.45 nicht im Repository (flacher Checkout)")
    assert _verstoesse(alt.stdout.lstrip("\ufeff")) == [
        "kopieren_im_try", "start_im_try", "ruecknahme_im_catch", "exitcode_ungeprueft"]


_PYTHON_PRUEFUNG = ("Get-RelayPythonFassung", "Test-RelayPythonPasst")


def _python_lauf(tmp_path, funktionen: str) -> dict:
    grenzen = "\n".join(re.search(rf"^\${n} = .*$", _code(), re.M).group(0)
                        for n in ("RelayPythonMin", "RelayPythonMax"))
    skript = tmp_path / "py.ps1"
    skript.write_text("Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n" + grenzen + "\n"
                      + funktionen + "\n$e = [ordered]@{}\n"
                      "foreach ($v in @('3.10', '3.11', '3.12', '3.13', '3.14', '3.15', '', 'kaputt')) { $e[\"v$v\"] = Test-RelayPythonPasst $v }\n"
                      "$e['fehlt'] = Get-RelayPythonFassung 'C:\\gibt\\es\\nicht.exe' @()\n"
                      "ConvertTo-Json -Compress -InputObject $e\n", encoding="utf-8-sig")
    umgebung = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    r = subprocess.run([_ps(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(skript)],
                       capture_output=True, text=True, timeout=120, env=umgebung)
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(r.stdout.strip().splitlines()[-1])


_PYTHON_SOLL = {"v3.10": False, "v3.11": True, "v3.12": True, "v3.13": True, "v3.14": False,
                "v3.15": False, "v": False, "vkaputt": False, "fehlt": ""}


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_python_nur_fassungen_mit_wheels(tmp_path):
    """CI 0.2.46: `py -3` lieferte auf dem Runner Python 3.15; pywin32 311 hat dafür kein Wheel,
    pip scheiterte. Nur 3.11–3.13 zählen; ein fehlendes Programm ist kein Abbruch."""
    assert _python_lauf(tmp_path, "\n".join(_funktion(n) for n in _PYTHON_PRUEFUNG)) == _PYTHON_SOLL


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_python_gegenprobe_ohne_obergrenze(tmp_path):
    funktionen = "\n".join(_funktion(n) for n in _PYTHON_PRUEFUNG)
    ohne = funktionen.replace("-and ([version]$Fassung -le [version]$RelayPythonMax)", "")
    assert ohne != funktionen
    e = _python_lauf(tmp_path, ohne)
    assert e["v3.15"] is True and e != _PYTHON_SOLL


def test_python_suche_fragt_unterfassungen_zuerst():
    code = _code()
    assert '@("-3.13", "-3.12", "-3.11", "-3")' in code
    assert "Test-RelayPythonPasst $ver" in code
