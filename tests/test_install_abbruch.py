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
import sys

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
        + _teile() + "\n$RelayMerkerName = \"zurueckholen.json\"\n"
        + "\n".join(_funktion(n) for n in ("Invoke-RelayNativ", "Get-RelaySicherungsTeile", "Test-RelayVerschiebbar",
                                             "Write-RelayMerker", "Read-RelayMerker", "Get-RelayTeilFassung",
                                             "Get-RelayTeilLauf", "Set-RelayTeilMarke",
                                             "Get-RelayStartGrund", "Start-RelayDienst",
                                             "Move-RelayPfad", "Get-RelayFreierName", "Merge-RelayOrdner", "Repair-RelayReste",
                                             "Backup-RelayFassung", "Restore-RelayFassung", "Restore-RelayNachAbbruch"))
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
$nachSicherung = @(Get-ChildItem "{ziel}" -Name -Force | Sort-Object | ForEach-Object {{ "$_" }})
New-Item -ItemType Directory "{ziel}/app" | Out-Null
Set-Content "{ziel}/app/.relay-fassung" "0.2.46"
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
function Get-Service { param($Name) [pscustomobject]@{ Status = "Running" } }
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
$venv = "{ziel}/venv"; $umgebungGesetzt = $false; $firewallGesetzt = $false; $altPort = ""
$gesichert = @(Backup-RelayFassung -Ziel $InstallDir -Sicherung $sicherung -Teile $RelayTeile)
New-Item -ItemType Directory "{ziel}/app" | Out-Null
Set-Content "{ziel}/app/.relay-fassung" "0.2.46"
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
    start = h.find("Start-RelayDienst -DataDir $dataDir")
    if start < 0:
        start = h.find('Start-Service -Name "ExoSmtpRelay"')                 # Stände bis 0.2.52
    if not (versuch < start < fang) or versuch < 0:
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


def _dll_lauf(tmp_path, venv, basis, pfad: str) -> dict:
    skript = tmp_path / "dll.ps1"
    skript.write_text("Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
                      "$global:M = New-Object System.Collections.ArrayList\n"
                      "function Write-Ok([string]$m) { [void]$global:M.Add(\"OK: $m\") }\n"
                      + _funktion("Sync-RelayDienstDlls")
                      + f"\nSync-RelayDienstDlls -Venv '{venv}' -Basis '{basis}' -MaschinenPfad '{pfad}'\n"
                      f"ConvertTo-Json -Compress -InputObject ([ordered]@{{ Dateien = @(Get-ChildItem '{venv}' -Name | Sort-Object | ForEach-Object {{ \"$_\" }}); Meldungen = @($global:M) }})\n",
                      encoding="utf-8-sig")
    umgebung = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    r = subprocess.run([_ps(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(skript)],
                       capture_output=True, text=True, timeout=120, env=umgebung)
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_dienst_dlls_nur_ohne_path_und_wieder_weg(tmp_path):
    """CI 0.2.46: Python nicht im Maschinen-PATH → pythonservice.exe findet python313.dll nicht,
    der Dienst hängt bis zum Zeitlimit. Dann DLLs neben pythonservice.exe — und wieder weg,
    sobald die Basis im PATH steht (anlegen und entfernen über denselben Merker)."""
    basis, venv = tmp_path / "Python313", tmp_path / "venv"
    basis.mkdir()
    venv.mkdir()
    for n in ("python313.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll", "sqlite3.dll"):
        (basis / n).write_text(n)
    (venv / "pythonservice.exe").write_text("x")
    e = _dll_lauf(tmp_path, venv, basis, r"C:\Windows\system32;C:\Windows")
    assert sorted(e["Dateien"]) == ["python3.dll", "python313.dll", "pythonservice.exe", "relay-dienst-dlls.txt",
                                    "vcruntime140.dll", "vcruntime140_1.dll"], e
    e = _dll_lauf(tmp_path, venv, basis, rf"C:\Windows;{basis}\\")
    assert e["Dateien"] == ["pythonservice.exe"], e
    assert e["Meldungen"][-1].startswith("OK: Python-DLL ueber den Maschinen-PATH"), e


def test_dienst_dlls_vor_dem_start_abgeglichen():
    h = _hauptteil(_code())
    assert h.index("Sync-RelayDienstDlls -Venv $venv") < h.index("Start-RelayDienst -DataDir $dataDir")
    assert '[Environment]::GetEnvironmentVariable("Path", "Machine")' in h


_ABLAUF_VARS = '''
$InstallDir = "{z}"; $sicherung = "{z}/.install-alt"; $dataDir = "{z}/data"; $venv = "{z}/venv"
$venvPython = "{z}/venv/gibtsnicht"; $pipGelaufen = $false; $istUpgrade = $true; $dienstLief = $true
$altVersion = "0.2.44"; $umgebungGesetzt = $false; $firewallGesetzt = $false; $altPort = ""; $gesichert = @()
'''


def _installiert(ziel):
    (ziel / "app").mkdir(parents=True)
    (ziel / "app" / "main.py").write_text("alt")
    (ziel / "windows").mkdir()
    (ziel / "windows" / "service.py").write_text("# alt")
    (ziel / "VERSION").write_text("0.2.44")


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_gesperrter_teil_nichts_verschoben_meldung_stimmt(tmp_path):
    """Labtest 0.2.46 e3: windows\\ gesperrt. Der Sperrtest findet es VOR dem ersten Verschieben,
    app\\ bleibt am Platz, „unveraendert“ stimmt. (Sperre unter Linux nachgebaut; echt in
    „Installer echt“.)"""
    ziel = tmp_path / "inst"
    _installiert(ziel)
    e = _lauf(tmp_path, _DIENST_MOCK + _ABLAUF_VARS.format(z=ziel) + '''
function Test-RelayVerschiebbar([string]$Pfad) { -not $Pfad.EndsWith("windows") }
try { $g = @(Backup-RelayFassung -Ziel $InstallDir -Sicherung $sicherung -Teile $RelayTeile); $f = "" } catch { $f = "$($_.Exception.Message)" }
Restore-RelayNachAbbruch
ConvertTo-Json -Compress -InputObject ([ordered]@{ Fehler = $f; App = (Test-Path "$InstallDir/app/main.py");
    Sicherung = (Test-Path $sicherung); Meldungen = @($global:M) })
''')
    assert "gesperrt" in e["Fehler"] and "windows" in e["Fehler"] and "nichts verschoben" in e["Fehler"], e
    assert e["App"] is True and e["Sicherung"] is False, e
    assert any("nichts verschoben, die installierte Fassung ist unveraendert" in m for m in e["Meldungen"]), e


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_ruecknahme_liest_inhalt_der_sicherung(tmp_path):
    """Labtest 0.2.46 e3: app\\ lag schon in .install-alt, die Rückgabe von Backup ging verloren
    ($gesichert leer). Die Rücknahme richtet sich nach dem INHALT der Sicherung."""
    ziel = tmp_path / "inst"
    _installiert(ziel)
    (ziel / ".install-alt").mkdir()
    (ziel / "app").rename(ziel / ".install-alt" / "app")
    # Backup schreibt den Merker VOR dem ersten Verschieben — er liegt also auch hier.
    (ziel / ".install-alt" / "zurueckholen.json").write_text('{"teile":["app","windows","VERSION"],"fassung":"0.2.44","port":""}')
    e = _lauf(tmp_path, _DIENST_MOCK + _ABLAUF_VARS.format(z=ziel) + '''
$gesichert = @()
Restore-RelayNachAbbruch
ConvertTo-Json -Compress -InputObject ([ordered]@{ App = (Get-Content "$InstallDir/app/main.py" -Raw).Trim();
    Sicherung = (Test-Path $sicherung); Meldungen = @($global:M) })
''')
    assert e["App"] == "alt" and e["Sicherung"] is False, e
    assert any("Vorige Fassung 0.2.44 zurueckgeholt (app)" in m for m in e["Meldungen"]), e
    assert not any("unveraendert" in m for m in e["Meldungen"]), e


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_rest_eines_frueheren_laufs_wird_nicht_geloescht(tmp_path):
    """Labtest 0.2.46 e3, zweiter Lauf: „Rest … entfernt“ löschte die EINZIGE Sicherung von app\\.
    Jetzt kommt erst zurück, was am Platz fehlt; danach wird normal gesichert."""
    ziel = tmp_path / "inst"
    _installiert(ziel)
    (ziel / ".install-alt").mkdir()
    (ziel / "app").rename(ziel / ".install-alt" / "app")
    e = _lauf(tmp_path, f'''
$g = @(Backup-RelayFassung -Ziel "{ziel}" -Sicherung "{ziel}/.install-alt" -Teile $RelayTeile)
ConvertTo-Json -Compress -InputObject ([ordered]@{{ Gesichert = $g;
    Alt = (Get-Content "{ziel}/.install-alt/app/main.py" -Raw).Trim(); Meldungen = @($global:M) }})
''')
    assert e["Gesichert"] == ["app", "windows", "VERSION"], e
    assert e["Alt"] == "alt", e
    assert any("zurueckgeholt: app" in m for m in e["Meldungen"]), e


def test_lebenszeichen_vor_erfolg():
    """Labtest 0.2.46 e4: Dienst „Running“, /health leer, trotzdem „Aktualisiert“. Jetzt wartet
    der Installer im try auf /health mit der Soll-Version; sonst Rücknahme samt Port."""
    h = _hauptteil(_code())
    fang = h.index("\n} catch {\n")
    assert h.index("Start-RelayDienst -DataDir $dataDir") < h.index("Wait-RelayGesund -Python $venvPython -Port $WebPort") < fang
    r = _funktion("Restore-RelayNachAbbruch")
    assert '--setzen "PYTHONPATH=$sp;$sp\\win32;$sp\\win32\\lib" --port "$altPort"' in r
    assert "Sync-RelayFirewall -WebPort $altPort" in r
    assert "$firewallGesetzt = $true\n        Sync-RelayFirewall -WebPort $WebPort" in _code()
    assert "Get-RelaySicherungsTeile $sicherung $RelayTeile" in r
    assert "if (@($gesichert).Count -gt 0 -or (Read-RelayMerker $sicherung))" in r


_MOVE_SPERRE = '''
$global:Gesperrt = @()
function Move-RelayPfad([string]$Von, [string]$Nach) {
    $n = $Von.Replace('\\', '/')
    if (@($global:Gesperrt | ForEach-Object { $_.Replace('\\', '/') }) -contains $n) { throw "Der Prozess kann nicht auf die Datei zugreifen: $Von" }
    if (Test-Path -LiteralPath $Nach) { throw "Ziel existiert schon, nicht verschoben: $Nach" }
    if (Test-Path -LiteralPath $Von -PathType Container) { [System.IO.Directory]::Move($Von, $Nach) } else { [System.IO.File]::Move($Von, $Nach) } }
$global:Fw = New-Object System.Collections.ArrayList
function Sync-RelayFirewall([int]$WebPort) { [void]$global:Fw.Add($WebPort) }
'''


def _e5_zustand(ziel):
    """Nach e5: app\\ und VERSION zurück, windows\\ (neu, 9.9.8) gesperrt geblieben — die alte
    windows\\ liegt mit Merker in .install-alt."""
    _installiert(ziel)
    (ziel / "windows" / "alt.txt").write_text("alt")
    sich = ziel / ".install-alt"
    sich.mkdir()
    (ziel / "windows").rename(sich / "windows")
    (ziel / "windows").mkdir()
    (ziel / "windows" / ".relay-fassung").write_text("9.9.8")
    (sich / "zurueckholen.json").write_text('{"teile":["windows"],"fassung":"0.2.44","port":"8443"}')


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_rest_mit_merker_holt_alte_teile_auch_ueber_belegtes_ziel(tmp_path):
    """Labtest 0.2.48 e5b: „alle Teile am Platz“ hielt die verworfene NEUE windows\\ für richtig
    und löschte die Sicherung der alten. Mit Merker kommt die alte zurück."""
    ziel = tmp_path / "inst"
    _e5_zustand(ziel)
    e = _lauf(tmp_path, f'''
$g = @(Backup-RelayFassung -Ziel "{ziel}" -Sicherung "{ziel}/.install-alt" -Teile $RelayTeile -Fassung "0.2.44" -Port "8443")
ConvertTo-Json -Compress -InputObject ([ordered]@{{ Gesichert = $g;
    AltWindows = (Test-Path "{ziel}/.install-alt/windows/alt.txt"); Meldungen = @($global:M) }})
''')
    assert e["AltWindows"] is True, e                    # die ALTE windows\\ ist jetzt die Sicherung
    assert e["Gesichert"] == ["app", "windows", "VERSION"], e
    assert any("zurueckgeholt (Fassung 0.2.44): windows" in m for m in e["Meldungen"]), e


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_rest_mit_merker_weiter_gesperrt_bleibt_unangetastet(tmp_path):
    ziel = tmp_path / "inst"
    _e5_zustand(ziel)
    e = _lauf(tmp_path, _MOVE_SPERRE + f'''
$global:Gesperrt = @("{ziel}/windows")
try {{ Backup-RelayFassung -Ziel "{ziel}" -Sicherung "{ziel}/.install-alt" -Teile $RelayTeile | Out-Null; $f = "" }} catch {{ $f = "$($_.Exception.Message)" }}
ConvertTo-Json -Compress -InputObject ([ordered]@{{ Fehler = $f; Alt = (Test-Path "{ziel}/.install-alt/windows/alt.txt");
    Merker = (Test-Path "{ziel}/.install-alt/zurueckholen.json"); App = (Test-Path "{ziel}/app/main.py"); Meldungen = @($global:M) }})
''')
    assert "nicht zurueckholbar: windows" in e["Fehler"], e
    assert e["Alt"] and e["Merker"] and e["App"], e
    assert not any("entfernt" in m for m in e["Meldungen"]), e


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
@pytest.mark.parametrize("gesperrt, port_zurueck", [("windows", True), ("app", False)])
def test_port_folgt_app(tmp_path, gesperrt, port_zurueck):
    """Labtest 0.2.48 e5a: Firewall alt, WEBUI_PORT neu. Jetzt eine Regel für beides: zurück,
    wenn app\\ zurückkam, sonst keins."""
    ziel = tmp_path / "inst"
    _installiert(ziel)
    e = _lauf(tmp_path, _DIENST_MOCK + _MOVE_SPERRE + _ABLAUF_VARS.format(z=ziel) + f'''
$firewallGesetzt = $true; $altPort = "8443"
$gesichert = @(Backup-RelayFassung -Ziel $InstallDir -Sicherung $sicherung -Teile $RelayTeile -Fassung "0.2.44" -Port "8443")
New-Item -ItemType Directory "{ziel}/app", "{ziel}/windows" | Out-Null
Set-Content "{ziel}/app/.relay-fassung" "9.9.8"; Set-Content "{ziel}/windows/.relay-fassung" "9.9.8"
Set-Content "{ziel}/VERSION" "9.9.8"
$global:Gesperrt = @("{ziel}/{gesperrt}")
Restore-RelayNachAbbruch
ConvertTo-Json -Compress -InputObject ([ordered]@{{ Fw = @($global:Fw); Merker = (Get-Content "$sicherung/zurueckholen.json" -Raw | ConvertFrom-Json).teile;
    Meldungen = @($global:M) }})
''')
    assert e["Fw"] == ([8443] if port_zurueck else []), e
    assert e["Merker"] == gesperrt or e["Merker"] == [gesperrt], e
    assert any("UNVOLLSTAENDIG" in m for m in e["Meldungen"]), e
    if not port_zurueck:
        assert any("Web-Port bleibt wie eingestellt" in m for m in e["Meldungen"]), e


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_rest_ohne_merker_wird_nie_zurueckgeholt(tmp_path):
    """Labtest 0.2.49 Z2: Nach einem ERFOLGREICHEN Upgrade blieb .install-alt\\windows als leerer
    Rest (Sperre darin). Ein Folgelauf hielt ihn für eine Sicherung und verwarf das gute
    windows\\. Jetzt: ohne Merker und ohne eigenes Verschieben nichts zurückholen; ein leerer
    Ordner ist nie eine Sicherung."""
    ziel = tmp_path / "inst"
    _installiert(ziel)
    (ziel / "windows" / "gut.txt").write_text("gut")
    (ziel / ".install-alt" / "windows").mkdir(parents=True)                     # leerer Rest
    (ziel / ".install-alt" / "app").mkdir()
    (ziel / ".install-alt" / "app" / "irgendwas.py").write_text("x")          # Rest mit Inhalt, ohne Merker
    e = _lauf(tmp_path, _DIENST_MOCK + _ABLAUF_VARS.format(z=ziel) + '''
$leer = @(Get-RelaySicherungsTeile $sicherung $RelayTeile)
Restore-RelayNachAbbruch
ConvertTo-Json -Compress -InputObject ([ordered]@{ Teile = $leer; Gut = (Test-Path "$InstallDir/windows/gut.txt");
    App = (Get-Content "$InstallDir/app/main.py" -Raw).Trim(); Meldungen = @($global:M) })
''')
    assert e["Teile"] == ["app"], e                                     # leerer windows-Rest zählt nicht
    assert e["Gut"] is True and e["App"] == "alt", e
    assert not any("zurueckgeholt" in m for m in e["Meldungen"]), e


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_holen_scheitert_neues_kommt_zurueck(tmp_path):
    """Labtest 0.2.49 Z1: Sperre IN .install-alt\\windows. Das Neue war schon beiseite, das Alte
    kam nicht — kein windows\\ am Platz. Jetzt kommt das Neue zurück, der Teil bleibt offen."""
    ziel = tmp_path / "inst"
    _installiert(ziel)
    e = _lauf(tmp_path, _MOVE_SPERRE + f'''
$g = @(Backup-RelayFassung -Ziel "{ziel}" -Sicherung "{ziel}/.install-alt" -Teile $RelayTeile -Fassung "0.2.44" -Port "")
New-Item -ItemType Directory "{ziel}/app", "{ziel}/windows" | Out-Null
Set-Content "{ziel}/app/.relay-fassung" "9.9.8"; Set-Content "{ziel}/windows/.relay-fassung" "9.9.8"
Set-Content "{ziel}/windows/neu.txt" "neu"
$global:Gesperrt = @("{ziel}/.install-alt/windows")
$offen = @(Restore-RelayFassung -Ziel "{ziel}" -Sicherung "{ziel}/.install-alt" -Teile $RelayTeile)
ConvertTo-Json -Compress -InputObject ([ordered]@{{ Offen = $offen; Neu = (Test-Path "{ziel}/windows/neu.txt");
    App = (Get-Content "{ziel}/app/main.py" -Raw).Trim(); Merker = (Get-Content "{ziel}/.install-alt/zurueckholen.json" -Raw | ConvertFrom-Json).teile }})
''')
    assert e["Offen"] == ["windows"] or e["Offen"] == "windows", e
    assert e["Neu"] is True, e                                          # windows\\ am Platz (die neue)
    assert e["App"] == "alt", e
    assert e["Merker"] in ("windows", ["windows"]), e


def test_rest_nach_erfolg_als_warnung():
    code = _code()
    ende = code[code.index("\n} catch {\n", code.index("# -- Dienst anhalten")):]
    assert "nicht loeschbar (gesperrt?) - kein Fehler" in ende


def test_kein_move_item_im_installer():
    """Labtest 0.2.50: Move-Item auf Ordner ist unter PS 5.1 nicht atomar. Verschoben wird NUR über
    Move-RelayPfad ([IO.Directory]::Move / [IO.File]::Move)."""
    code = "\n".join(z for z in _code().splitlines() if not z.lstrip().startswith("#"))
    assert "Move-Item" not in code and "Rename-Item" not in code
    f = _funktion("Move-RelayPfad")
    assert "[System.IO.Directory]::Move($Von, $Nach)" in f and "Ziel existiert schon" in f


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_move_relaypfad_nie_in_ein_vorhandenes_ziel(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_text("x")
    (tmp_path / "b").mkdir()
    e = _lauf(tmp_path, f'''
try {{ Move-RelayPfad "{tmp_path}/a" "{tmp_path}/b"; $f = "" }} catch {{ $f = "$($_.Exception.Message)" }}
Move-RelayPfad "{tmp_path}/a" "{tmp_path}/c"
ConvertTo-Json -Compress -InputObject ([ordered]@{{ Fehler = $f; Drin = (Test-Path "{tmp_path}/b/a"); C = (Test-Path "{tmp_path}/c/x.txt") }})
''')
    assert "Ziel existiert schon" in e["Fehler"] and e["Drin"] is False and e["C"] is True, e


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_reste_des_teilverschiebens_werden_zurueckgefuehrt(tmp_path):
    """Labtest 0.2.50 Z3: Der Sperrtest von aa45770 (Move-Item) verschob 14 von 102 Dateien nach
    app.relay-sperrtest; ein Folgelauf legte app.relay-sperrtest\\app an. Repair-RelayReste führt
    beides dateiweise zurück, ohne Handarbeit."""
    ziel = tmp_path / "inst"
    _installiert(ziel)
    (ziel / "app" / "config.py").write_text("am Platz")
    r = ziel / "app.relay-sperrtest"
    (r / "webui").mkdir(parents=True)
    (r / "settings_store.py").write_text("teil1")
    (r / "webui" / "app.py").write_text("teil1")
    (r / "app" / "routen").mkdir(parents=True)
    (r / "app" / "handler.py").write_text("teil2")
    (r / "app" / "routen" / "x.py").write_text("teil2")
    (r / "app" / "config.py").write_text("doppelt")
    (ziel / "windows.relay-verworfen").mkdir()
    (ziel / "windows.relay-verworfen" / "neu.py").write_text("neu")
    e = _lauf(tmp_path, f'''
Repair-RelayReste "{ziel}" $RelayTeile
ConvertTo-Json -Compress -InputObject ([ordered]@{{ Dateien = @(Get-ChildItem "{ziel}/app" -Recurse -File -Force | ForEach-Object {{ $_.FullName.Substring("{ziel}/app".Length + 1).Replace('\\', '/') }} | Sort-Object);
    RestDa = (Test-Path "{ziel}/app.relay-sperrtest"); Verworfen = (Test-Path "{ziel}/windows.relay-verworfen"); Meldungen = @($global:M) }})
''')
    assert e["Dateien"] == ["config.py", "handler.py", "main.py", "routen/x.py", "settings_store.py", "webui/app.py"], e
    assert e["RestDa"] is True, e                             # nur das Doppelte bleibt liegen
    assert e["Verworfen"] is False, e                         # windows\\ am Platz → verworfene weg
    assert any("zurueckgefuehrt" in m and "doppelt" in m for m in e["Meldungen"]), e


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_teilweise_alte_fassung_wird_ergaenzt(tmp_path):
    """Labtest 0.2.50 Z1b: windows\\ am Platz unvollständig (alte Fassung, Teilverschiebung), Rest in
    .install-alt\\windows. Die Rücknahme ergänzt die fehlenden Dateien statt „zurueckgeholt“ zu
    behaupten oder die Teile zu verwerfen."""
    ziel = tmp_path / "inst"
    _installiert(ziel)
    (ziel / "windows" / ".relay-fassung").write_text("0.2.44")
    sich = ziel / ".install-alt"
    (sich / "windows").mkdir(parents=True)
    (sich / "windows" / "install.ps1").write_text("alt")
    (sich / "zurueckholen.json").write_text('{"teile":["windows"],"fassung":"0.2.44","port":""}')
    e = _lauf(tmp_path, f'''
$offen = @(Restore-RelayFassung -Ziel "{ziel}" -Sicherung "{sich}" -Teile $RelayTeile)
ConvertTo-Json -Compress -InputObject ([ordered]@{{ Offen = $offen; Datei = (Test-Path "{ziel}/windows/install.ps1");
    Service = (Test-Path "{ziel}/windows/service.py"); Sicherung = (Test-Path "{sich}"); Meldungen = @($global:M) }})
''')
    assert e["Offen"] == [] and e["Datei"] and e["Service"] and e["Sicherung"] is False, e
    assert any("1 fehlende Datei(en) aus der Sicherung ergaenzt" in m for m in e["Meldungen"]), e


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Sperrsemantik offener Dateien nur unter Windows")
def test_verschieben_mit_offener_datei_bewegt_nichts(tmp_path):
    """Labtest 0.2.50, isoliert unter Windows PowerShell 5.1: Ordner mit 60 Dateien, eine offen
    (FileShare Read, kein Delete). Move-RelayPfad scheitert und bewegt 0 Dateien. Gegenprobe:
    Move-Item bewegt TEILWEISE (der Fehler, den 0.2.51 behebt)."""
    q = tmp_path / "q"
    skript = tmp_path / "offen.ps1"
    skript.write_text("Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
                      + _funktion("Move-RelayPfad") + f"""
function Neu([string]$d) {{
    if (Test-Path $d) {{ Remove-Item -Recurse -Force $d }}
    New-Item -ItemType Directory "$d\\sub" | Out-Null
    0..59 | ForEach-Object {{ Set-Content "$d\\$(if ($_ % 2) {{ 'sub\\' }})f$_.py" "$_" }}
}}
function Lauf([scriptblock]$b, [string]$d) {{
    Neu $d
    $h = [System.IO.File]::Open("$d\\f30.py", 'Open', 'Read', 'Read')
    try {{ try {{ & $b; $f = "kein Fehler" }} catch {{ $f = "Fehler" }} }} finally {{ $h.Close() }}
    $ziel = "$d.neu"
    [ordered]@{{ Fehler = $f; Quelle = @(Get-ChildItem $d -Recurse -File -ErrorAction SilentlyContinue).Count;
                 Ziel = @(Get-ChildItem $ziel -Recurse -File -ErrorAction SilentlyContinue).Count }}
}}
$a = Lauf {{ Move-RelayPfad "{q}" "{q}.neu" }} "{q}"
$b = Lauf {{ Move-Item -LiteralPath "{q}2" -Destination "{q}2.neu" -ErrorAction Stop }} "{q}2"
ConvertTo-Json -Compress -InputObject ([ordered]@{{ Relay = $a; MoveItem = $b }})
""", encoding="utf-8-sig")
    umgebung = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    r = subprocess.run([shutil.which("powershell"), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(skript)], capture_output=True, text=True, timeout=120, env=umgebung)
    assert r.returncode == 0, r.stderr + r.stdout
    e = json.loads(r.stdout.strip().splitlines()[-1])
    assert e["Relay"] == {"Fehler": "Fehler", "Quelle": 60, "Ziel": 0}, e
    # Gegenprobe: Move-Item hat teilweise verschoben (Labtest 0.2.50 auf 26100).
    assert e["MoveItem"]["Fehler"] == "Fehler" and 0 < e["MoveItem"]["Ziel"] < 60, e


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_backup_heilt_reste_vor_dem_sperrtest(tmp_path):
    """Der Weg im Installer: Backup-RelayFassung führt Reste zurück, BEVOR es prüft und sichert —
    sonst sicherte es ein zerlegtes app\\ (Labtest 0.2.50 Z3)."""
    ziel = tmp_path / "inst"
    _installiert(ziel)
    (ziel / "app.relay-sperrtest").mkdir()
    (ziel / "app.relay-sperrtest" / "config.py").write_text("x")
    e = _lauf(tmp_path, f'''
$g = @(Backup-RelayFassung -Ziel "{ziel}" -Sicherung "{ziel}/.install-alt" -Teile $RelayTeile -Fassung "0.2.44" -Port "")
ConvertTo-Json -Compress -InputObject ([ordered]@{{ Config = (Test-Path "{ziel}/.install-alt/app/config.py");
    Rest = (Test-Path "{ziel}/app.relay-sperrtest") }})
''')
    assert e == {"Config": True, "Rest": False}, e



def _z4_lauf(tmp_path, lauf: str) -> dict:
    """Gleiche Fassung (0.2.51) noch einmal, neues app\\ kaputt; Rücknahme des eigenen Laufs."""
    ziel = tmp_path / "inst"
    _installiert(ziel)
    for n in ("a.py", "b.py", "c.py"):
        (ziel / "app" / n).write_text("alt-" + n)
    (ziel / "app" / ".relay-fassung").write_text("0.2.51\nlauf=aaaa\n")
    e = _lauf(tmp_path, f"""
$g = @(Backup-RelayFassung -Ziel "{ziel}" -Sicherung "{ziel}/.install-alt" -Teile $RelayTeile -Fassung "0.2.51" -Port "" -Lauf "{lauf}")
New-Item -ItemType Directory "{ziel}/app" | Out-Null
Set-RelayTeilMarke "{ziel}/app" "0.2.51" "bbbb"
Set-Content "{ziel}/app/main.py" "kaputt"
Set-Content "{ziel}/app/a.py" "neu"
$offen = @(Restore-RelayFassung -Ziel "{ziel}" -Sicherung "{ziel}/.install-alt" -Teile $RelayTeile)
ConvertTo-Json -Compress -InputObject ([ordered]@{{ Offen = $offen;
    Dateien = @(Get-ChildItem "{ziel}/app" -File -Force | Sort-Object Name | ForEach-Object {{ "$($_.Name)=$((Get-Content $_.FullName -Raw).Trim())" }});
    Meldungen = @($global:M) }})
""")
    return e


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_gleiche_fassung_eigener_lauf_wird_getauscht_nicht_ergaenzt(tmp_path):
    """Labtest 0.2.51 Z4: dieselbe Fassung neu installiert, die neue wirft. „41 fehlende Dateien
    ergänzt“, app\\ hatte danach einen anderen Inhalt. Jetzt erkennt die Rücknahme an der Lauf-ID
    den NEUEN Stand des eigenen Laufs und tauscht vollständig."""
    e = _z4_lauf(tmp_path, "bbbb")
    e["Dateien"] = [d.replace("\r\n", "\n") for d in e["Dateien"]]          # Windows schreibt CRLF
    assert e["Dateien"] == [".relay-fassung=0.2.51\nlauf=aaaa", "a.py=alt-a.py", "b.py=alt-b.py",
                            "c.py=alt-c.py", "main.py=alt"], e
    assert not any("ergaenzt" in m for m in e["Meldungen"]), e


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_gleiche_fassung_gegenprobe_ohne_lauf_id(tmp_path):
    """Trennscharf: ohne Lauf-ID (Stand 3095166) entscheidet nur die Fassung — gleiche Fassung gilt als
    „alt“, es wird nur ergänzt, das kaputte main.py und a.py=neu bleiben."""
    e = _z4_lauf(tmp_path, "")
    assert "main.py=kaputt" in e["Dateien"] and "a.py=neu" in e["Dateien"], e



_START_MOCK = """
$global:Starts = 0; $global:ScheitertBis = 0
function Start-Service { param($Name) $global:Starts++; if ($global:Starts -le $global:ScheitertBis) { throw "Failed to start service 'EXO SMTP Relay (ExoSmtpRelay)'." } }
function Stop-Service { param($Name, [switch]$Force) }
function Get-Service { param($Name) [pscustomobject]@{ Status = "Running" } }
function Start-Sleep { param($Seconds) }
"""


def _start_lauf(tmp_path, scheitert_bis: int, funktionen: str | None = None) -> dict:
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "service.log").write_text("ModuleNotFoundError: No module named 'x'\n")
    skript = tmp_path / "start.ps1"
    skript.write_text("Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
                      "$global:M = New-Object System.Collections.ArrayList\n"
                      "function Write-Warn([string]$m) { [void]$global:M.Add(\"WARN: $m\") }\n"
                      + (funktionen or (_funktion("Get-RelayStartGrund") + "\n" + _funktion("Start-RelayDienst")))
                      + _START_MOCK + f"$global:ScheitertBis = {scheitert_bis}\n"
                      f"try {{ Start-RelayDienst -DataDir '{data}'; $f = '' }} catch {{ $f = \"$($_.Exception.Message)\" }}\n"
                      "ConvertTo-Json -Compress -InputObject ([ordered]@{ Fehler = $f; Starts = $global:Starts; Meldungen = @($global:M) })\n",
                      encoding="utf-8-sig")
    umgebung = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    r = subprocess.run([_ps(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(skript)],
                       capture_output=True, text=True, timeout=120, env=umgebung)
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_dienststart_wiederholt_und_meldet_den_grund(tmp_path):
    """Release v0.2.52-beta: einmal „Failed to start service“ auf dem Runner, in vier Wiederholungen
    nicht wieder. Ein einzelner Fehlschlag kippt die Installation nicht mehr; er steht mit Grund
    (SCM-Ereignis, service.log) in der Ausgabe."""
    e = _start_lauf(tmp_path, 1)
    assert e["Fehler"] == "" and e["Starts"] == 2, e
    assert any("Versuch 1 von 3: Failed to start service" in m and "service.log: ModuleNotFoundError" in m
               for m in e["Meldungen"]), e


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_dienststart_scheitert_ehrlich_nach_allen_versuchen(tmp_path):
    e = _start_lauf(tmp_path, 9)
    assert e["Starts"] == 3 and e["Fehler"].startswith("Dienst startet nicht (3 Versuche): Failed to start service"), e
    assert "service.log: ModuleNotFoundError" in e["Fehler"], e


@pytest.mark.skipif(not _ps(), reason="keine PowerShell vorhanden")
def test_dienststart_gegenprobe_ohne_wiederholung(tmp_path):
    """Trennscharf: wie bis 0.2.52 nur ein Versuch — ein einzelner Fehlschlag bricht ab."""
    ein = _funktion("Get-RelayStartGrund") + "\n" + _funktion("Start-RelayDienst").replace(
        "[int]$Versuche = 3", "[int]$Versuche = 1")
    e = _start_lauf(tmp_path, 1, ein)
    assert e["Fehler"] != "" and e["Starts"] == 1, e
