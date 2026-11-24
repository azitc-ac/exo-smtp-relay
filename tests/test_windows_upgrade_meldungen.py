"""Labtest 0.2.43 (Windows Server 2025): zwei Upgrade-Befunde.

1. install.ps1 meldete nach einem UPGRADE weiter „admin / admin - bitte aendern“,
   obwohl das Kennwort längst geändert war. Jetzt fragt der Installer dieselbe
   Regel wie die Oberfläche (`app/kennwort_stand.py`).
2. data\\logs\\service.log (stdout/stderr des Kindprozesses, geschrieben vom
   Dienst-Wrapper windows/service.py) stand in cp1252 („�bersprungen“), app.log
   in UTF-8. Der Wrapper setzt jetzt PYTHONIOENCODING=utf-8 für das Kind.
"""
import ast
import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from conftest import APP, WURZEL

STAND = APP / "kennwort_stand.py"


# ── 1. Kennwort-Hinweis ────────────────────────────────────────────────────────

def _stand(settings: Path, **env) -> subprocess.CompletedProcess:
    umgebung = {k: v for k, v in os.environ.items() if k != "WEBUI_PASSWORD"}
    umgebung.update(env)
    return subprocess.run([sys.executable, str(STAND), str(settings)],
                          capture_output=True, text=True, env=umgebung)


def test_eigenes_kennwort_gesetzt(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"ADMIN_PASSWORD_HASH": "salt$abc"}), encoding="utf-8")
    r = _stand(s)
    assert (r.returncode, r.stdout.strip(), r.stderr) == (0, "eigenes", "")


@pytest.mark.parametrize("inhalt", [None, {"ADMIN_PASSWORD_HASH": ""}, {"ANDERES": 1}])
def test_noch_vorgabe(tmp_path, inhalt):
    s = tmp_path / "settings.json"
    if inhalt is not None:
        s.write_text(json.dumps(inhalt), encoding="utf-8")
    r = _stand(s)
    assert (r.returncode, r.stdout.strip()) == (0, "vorgabe")


def test_eigenes_kennwort_aus_umgebung(tmp_path):
    assert _stand(tmp_path / "fehlt.json", WEBUI_PASSWORD="sehr-eigen").stdout.strip() == "eigenes"
    assert _stand(tmp_path / "fehlt.json", WEBUI_PASSWORD="changeme").stdout.strip() == "vorgabe"


def test_unlesbar_meldet_unbekannt_ohne_stderr(tmp_path):
    """PowerShell 5.1 bricht bei stderr eines Programms unter ErrorActionPreference=Stop ab."""
    s = tmp_path / "settings.json"
    s.write_text("{kaputt", encoding="utf-8")
    r = _stand(s)
    assert (r.returncode, r.stdout.strip(), r.stderr) == (0, "unbekannt", "")


def test_kennwort_stand_nur_standardbibliothek():
    """install.ps1 ruft es direkt mit dem venv-Python auf — keine Anwendungsmodule."""
    baum = ast.parse(STAND.read_text(encoding="utf-8"))
    module = {a.name.split(".")[0] for k in ast.walk(baum) if isinstance(k, ast.Import) for a in k.names}
    module |= {k.module.split(".")[0] for k in ast.walk(baum) if isinstance(k, ast.ImportFrom)}
    assert module <= {"__future__", "json", "os", "sys", "pathlib"}, module


def test_oberflaeche_und_installer_teilen_eine_regel(monkeypatch):
    from webui import deps
    import kennwort_stand
    aufrufe = []
    monkeypatch.setattr(kennwort_stand, "aenderung_noetig", lambda h, p: aufrufe.append((h, p)) or True)
    monkeypatch.setattr(deps.settings_store, "get", lambda k, *a, **kw: "h" if k == "ADMIN_PASSWORD_HASH" else None)
    assert deps._password_change_required() is True and aufrufe == [("h", deps.config.WEBUI_PASSWORD)]


def test_install_ps1_meldet_kennwort_nach_regel():
    ps = (WURZEL / "windows" / "install.ps1").read_text(encoding="utf-8-sig")
    assert "app\\kennwort_stand.py" in ps
    schluss = [z for z in ps.splitlines() if "Weboberflaeche: https://" in z]
    assert len(schluss) == 1, schluss
    assert "$kennwortHinweis" in schluss[0] and "admin / admin" not in schluss[0], schluss[0]
    # „admin / admin“ nur noch im Zweig für das Vorgabe-Kennwort.
    zweige = [z for z in ps.splitlines() if "admin / admin - bitte aendern" in z]
    assert zweige == ['    $kennwortHinweis = "admin / admin - bitte aendern"'], zweige


# ── 2. service.log in UTF-8 ────────────────────────────────────────────────────

def _service_modul(monkeypatch):
    for name in ("servicemanager", "win32event", "win32service"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    su = types.ModuleType("win32serviceutil")
    su.ServiceFramework = type("ServiceFramework", (), {"__init__": lambda self, args: None})
    monkeypatch.setitem(sys.modules, "win32serviceutil", su)
    spec = importlib.util.spec_from_file_location("_relay_service", WURZEL / "windows" / "service.py")
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def test_service_log_ist_utf8_auch_bei_ansi_codepage(tmp_path, monkeypatch):
    service = _service_modul(monkeypatch)
    app = tmp_path / "app"
    app.mkdir()
    # Wie main.py: basicConfig schreibt auf stderr, das der Wrapper in service.log umleitet.
    (app / "main.py").write_text(
        "import logging\nlogging.basicConfig(format='%(message)s')\n"
        "logging.getLogger('t').warning('EXO-Postfachliste übersprungen — Grund')\n",
        encoding="utf-8")
    monkeypatch.setattr(service, "APP", app)
    monkeypatch.setattr(service, "VENV", tmp_path / "keine-venv")
    monkeypatch.setattr(service, "DATA", tmp_path / "data")
    monkeypatch.setattr(service, "LOGDATEI", tmp_path / "data" / "logs" / "service.log")
    # Ohne Konsole nimmt Python unter Windows die ANSI-Codepage — hier nachgestellt.
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    proc = service.RelayService.__new__(service.RelayService)._starten()
    assert proc.wait(timeout=60) == 0
    roh = (tmp_path / "data" / "logs" / "service.log").read_bytes()
    assert "übersprungen — Grund".encode("utf-8") in roh, roh


# ── 3. Dienst-Umgebung beim Upgrade zusammenführen, Port behalten ──────────────
# Bestehende Installationen ohne nachgeladene Wurzeln MÜSSEN für 0.2.43+ einmal
# install.ps1 ausführen — dieser Weg darf weder Port noch eigene Einträge verlieren.

def _umgebung():
    spec = importlib.util.spec_from_file_location("_dienst_umgebung",
                                                  WURZEL / "windows" / "dienst_umgebung.py")
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def _lauf(alt, *args):
    du = _umgebung()
    geschrieben = []
    rc = du.main(["dienst_umgebung.py", "--setzen", "PYTHONPATH=C:\\neu\\sp", *args],
                 lesen=lambda: list(alt), schreiben=geschrieben.append)
    return rc, geschrieben


def test_upgrade_behaelt_eigene_eintraege_und_port(capsys):
    alt = ["PYTHONPATH=C:\\alt\\sp", "WEBUI_PORT=9443", "WEBUI_PASSWORD=geheim", "EIGENE=1"]
    rc, geschrieben = _lauf(alt)
    assert (rc, capsys.readouterr().out.strip()) == (0, "9443")
    assert geschrieben == [["PYTHONPATH=C:\\neu\\sp", "WEBUI_PORT=9443",
                            "WEBUI_PASSWORD=geheim", "EIGENE=1"]]


def test_ausdruecklicher_port_gewinnt(capsys):
    rc, geschrieben = _lauf(["WEBUI_PORT=9443", "WEBUI_PASSWORD=geheim"], "--port", "10443")
    assert (rc, capsys.readouterr().out.strip()) == (0, "10443")
    assert geschrieben == [["WEBUI_PORT=10443", "WEBUI_PASSWORD=geheim", "PYTHONPATH=C:\\neu\\sp"]]


def test_erstinstallation_8443(capsys):
    rc, geschrieben = _lauf([])
    assert (rc, capsys.readouterr().out.strip()) == (0, "8443")
    assert geschrieben == [["PYTHONPATH=C:\\neu\\sp", "WEBUI_PORT=8443"]]


def test_doppelte_und_klein_geschriebene_installer_schluessel(capsys):
    rc, geschrieben = _lauf(["webui_port=7443", "Pythonpath=x", "PYTHONPATH=y", "A=b=c"])
    assert rc == 0 and capsys.readouterr().out.strip() == "7443"
    assert geschrieben == [["WEBUI_PORT=7443", "PYTHONPATH=C:\\neu\\sp", "A=b=c"]]


def test_fehler_ohne_schreiben_und_ohne_stderr(capsys):
    rc, geschrieben = _lauf(["WEBUI_PORT=9443"], "--port", "99999")
    aus = capsys.readouterr()
    assert rc == 1 and geschrieben == [] and aus.err == "" and "Fehler" in aus.out


def test_install_ps1_fuehrt_umgebung_zusammen():
    ps = (WURZEL / "windows" / "install.ps1").read_text(encoding="utf-8-sig")
    code = "\n".join(z for z in ps.splitlines() if not z.lstrip().startswith("#"))
    assert "windows\\dienst_umgebung.py" in code
    # Kein direktes Überschreiben des Registry-Werts mehr.
    assert '-Name "Environment"' not in code and "Set-ItemProperty" not in code
    # Port nur bei ausdrücklichem -WebPort weiterreichen, danach den wirksamen übernehmen.
    assert '$PSBoundParameters.ContainsKey("WebPort")' in code
    assert "$WebPort = [int]($umgebungAusgabe[-1])" in code
    assert "$LASTEXITCODE -ne 0" in code
    # Zuweisung vor der ersten Nutzung (Firewall).
    assert code.index("$WebPort = [int]") < code.index("Sync-RelayFirewall -WebPort $WebPort")


# ── 4. Firewall: Upgrade öffnet nie weiter als vorher ──────────────────────────
# Bis 0.2.43 löschte install.ps1 die Relay-Regeln und legte sie mit -Profile Any
# ohne RemoteAddress neu an — eigene Einschränkungen gingen verloren.

import re
import shutil

INSTALL = WURZEL / "windows" / "install.ps1"

# Nachbau der Cmdlets: Funktionen haben in PowerShell Vorrang vor Cmdlets.
_MOCKS = r'''
$global:Regeln = New-Object System.Collections.ArrayList
$global:Nr = 0
function Write-Ok([string]$m) { }
function Get-NetFirewallRule { [CmdletBinding()] param([string]$DisplayName)
    @($global:Regeln | Where-Object { $_.DisplayName -like $DisplayName }) }
function New-NetFirewallRule { [CmdletBinding()] param($DisplayName, $Direction, $Protocol, $LocalPort, $Action, $Profile)
    $global:Nr++
    [void]$global:Regeln.Add([pscustomobject]@{ Name = "neu$($global:Nr)"; DisplayName = $DisplayName;
        LocalPort = "$LocalPort"; RemoteAddress = "Any"; Profile = "$Profile"; Enabled = "True" })
    "erzeugt" }
function Remove-NetFirewallRule { [CmdletBinding()] param([string]$Name)
    $weg = @($global:Regeln | Where-Object { $_.Name -eq $Name }); foreach ($r in $weg) { $global:Regeln.Remove($r) } }
function Get-NetFirewallPortFilter { [CmdletBinding()] param($AssociatedNetFirewallRule)
    [pscustomobject]@{ Regel = $AssociatedNetFirewallRule; LocalPort = $AssociatedNetFirewallRule.LocalPort } }
function Get-NetFirewallAddressFilter { [CmdletBinding()] param($AssociatedNetFirewallRule)
    [pscustomobject]@{ RemoteAddress = $AssociatedNetFirewallRule.RemoteAddress } }
function Set-NetFirewallPortFilter { [CmdletBinding()] param($InputObject, $LocalPort)
    $InputObject.Regel.LocalPort = "$LocalPort" }
function Set-NetFirewallRule { [CmdletBinding()] param([string]$Name, [string]$NewDisplayName)
    foreach ($r in @($global:Regeln | Where-Object { $_.Name -eq $Name })) { $r.DisplayName = $NewDisplayName } }
function Regel($n, $dn, $port, $ra, $prof, $en) {
    [void]$global:Regeln.Add([pscustomobject]@{ Name = $n; DisplayName = $dn; LocalPort = "$port";
        RemoteAddress = $ra; Profile = $prof; Enabled = $en }) }
'''

# Die Logik bis 0.2.43 — nur für die eingebaute Gegenprobe.
_ALT = r'''
function Sync-RelayFirewall([int]$WebPort) {
    foreach ($regel in @(@{ Name = "EXO SMTP Relay - SMTP 25"; Port = 25 },
                         @{ Name = "EXO SMTP Relay - Web $WebPort"; Port = $WebPort })) {
        foreach ($r in @(Get-NetFirewallRule -DisplayName $regel.Name -ErrorAction SilentlyContinue)) { Remove-NetFirewallRule -Name $r.Name }
        New-NetFirewallRule -DisplayName $regel.Name -Direction Inbound -Protocol TCP `
            -LocalPort $regel.Port -Action Allow -Profile Any | Out-Null
    }
}
'''

_BESTAND = {
    "leer": "",
    "eingeschraenkt": ('Regel "s1" "EXO SMTP Relay - SMTP 25" 25 "10.0.0.0/8" "Domain" "True"\n'
                       'Regel "w1" "EXO SMTP Relay - Web 8443" 8443 "10.1.0.0/16" "Domain" "False"\n'),
    "zwei_web": ('Regel "s1" "EXO SMTP Relay - SMTP 25" 25 "10.0.0.0/8" "Domain" "True"\n'
                 'Regel "w1" "EXO SMTP Relay - Web 8443" 8443 "10.1.0.0/16" "Domain" "True"\n'
                 'Regel "w2" "EXO SMTP Relay - Web 9443" 9443 "10.2.0.0/16" "Private" "True"\n'),
    # Labtest 0.2.44: eine eingeschraenkte alte Regel und eine offene. Die offene passt sogar zum
    # neuen Port - behalten werden muss trotzdem die eingeschraenkte (nie weiter oeffnen als vorher).
    "offen_und_eingeschraenkt": ('Regel "s1" "EXO SMTP Relay - SMTP 25" 25 "Any" "Any" "True"\n'
                                 'Regel "w1" "EXO SMTP Relay - Web 8443" 8443 "10.1.0.0/16" "Domain" "True"\n'
                                 'Regel "w2" "EXO SMTP Relay - Web 9443" 9443 "Any" "Any" "True"\n'),
    # Nur das Profil eingeschraenkt, offene Regel zuerst und auf einem dritten Port.
    "offen_zuerst": ('Regel "s1" "EXO SMTP Relay - SMTP 25" 25 "Any" "Any" "True"\n'
                     'Regel "w0" "EXO SMTP Relay - Web 7443" 7443 "Any" "Domain, Private, Public" "True"\n'
                     'Regel "w1" "EXO SMTP Relay - Web 8443" 8443 "Any" "Domain" "True"\n'),
}


def _ps() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _funktion() -> str:
    ps = INSTALL.read_text(encoding="utf-8-sig")
    m = re.search(r"^function Sync-RelayFirewall\b.*?^\}", ps, re.S | re.M)
    assert m, "Sync-RelayFirewall fehlt in install.ps1"
    return m.group(0)


def _firewall(tmp_path, funktion: str, bestand: str, port: int) -> list[dict]:
    skript = tmp_path / "fw.ps1"
    skript.write_text("Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
                      + _MOCKS + funktion + "\n" + _BESTAND[bestand]
                      + f"Sync-RelayFirewall -WebPort {port}\n"
                      + "ConvertTo-Json -Compress -InputObject @($global:Regeln | Sort-Object DisplayName)\n",
                      encoding="utf-8-sig")
    r = subprocess.run([_ps(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(skript)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr + r.stdout
    return [{k: e[k] for k in ("DisplayName", "LocalPort", "RemoteAddress", "Profile", "Enabled")}
            for e in json.loads(r.stdout.strip().splitlines()[-1])]


def _pruefen(tmp_path, funktion):
    """Alle Erwartungen; liefert die Liste der verletzten (leer = alles gut)."""
    fehler = []
    if _firewall(tmp_path, funktion, "leer", 8443) != [
            {"DisplayName": "EXO SMTP Relay - SMTP 25", "LocalPort": "25", "RemoteAddress": "Any", "Profile": "Any", "Enabled": "True"},
            {"DisplayName": "EXO SMTP Relay - Web 8443", "LocalPort": "8443", "RemoteAddress": "Any", "Profile": "Any", "Enabled": "True"}]:
        fehler.append("erstinstallation")
    if _firewall(tmp_path, funktion, "eingeschraenkt", 8443) != [
            {"DisplayName": "EXO SMTP Relay - SMTP 25", "LocalPort": "25", "RemoteAddress": "10.0.0.0/8", "Profile": "Domain", "Enabled": "True"},
            {"DisplayName": "EXO SMTP Relay - Web 8443", "LocalPort": "8443", "RemoteAddress": "10.1.0.0/16", "Profile": "Domain", "Enabled": "False"}]:
        fehler.append("upgrade_gleicher_port")
    if _firewall(tmp_path, funktion, "eingeschraenkt", 9443) != [
            {"DisplayName": "EXO SMTP Relay - SMTP 25", "LocalPort": "25", "RemoteAddress": "10.0.0.0/8", "Profile": "Domain", "Enabled": "True"},
            {"DisplayName": "EXO SMTP Relay - Web 9443", "LocalPort": "9443", "RemoteAddress": "10.1.0.0/16", "Profile": "Domain", "Enabled": "False"}]:
        fehler.append("upgrade_portwechsel")
    if _firewall(tmp_path, funktion, "zwei_web", 9443) != [
            {"DisplayName": "EXO SMTP Relay - SMTP 25", "LocalPort": "25", "RemoteAddress": "10.0.0.0/8", "Profile": "Domain", "Enabled": "True"},
            {"DisplayName": "EXO SMTP Relay - Web 9443", "LocalPort": "9443", "RemoteAddress": "10.2.0.0/16", "Profile": "Private", "Enabled": "True"}]:
        fehler.append("genau_eine_web_regel")
    if _firewall(tmp_path, funktion, "offen_und_eingeschraenkt", 9443) != [
            {"DisplayName": "EXO SMTP Relay - SMTP 25", "LocalPort": "25", "RemoteAddress": "Any", "Profile": "Any", "Enabled": "True"},
            {"DisplayName": "EXO SMTP Relay - Web 9443", "LocalPort": "9443", "RemoteAddress": "10.1.0.0/16", "Profile": "Domain", "Enabled": "True"}]:
        fehler.append("eingeschraenkte_schlaegt_offene")
    if _firewall(tmp_path, funktion, "offen_zuerst", 9443) != [
            {"DisplayName": "EXO SMTP Relay - SMTP 25", "LocalPort": "25", "RemoteAddress": "Any", "Profile": "Any", "Enabled": "True"},
            {"DisplayName": "EXO SMTP Relay - Web 9443", "LocalPort": "9443", "RemoteAddress": "Any", "Profile": "Domain", "Enabled": "True"}]:
        fehler.append("profil_zaehlt_als_einschraenkung")
    return fehler


@pytest.mark.skipif(not _ps(), reason="keine PowerShell (pwsh/powershell) vorhanden")
def test_firewall_upgrade_behaelt_einschraenkungen(tmp_path):
    assert _pruefen(tmp_path, _funktion()) == []


@pytest.mark.skipif(not _ps(), reason="keine PowerShell (pwsh/powershell) vorhanden")
def test_firewall_gegenprobe_alte_logik_faellt_durch(tmp_path):
    """Die Prüfung ist trennscharf: die Logik bis 0.2.43 verletzt genau die Upgrade-Fälle."""
    assert _pruefen(tmp_path, _ALT) == ["upgrade_gleicher_port", "upgrade_portwechsel", "genau_eine_web_regel",
                                        "eingeschraenkte_schlaegt_offene", "profil_zaehlt_als_einschraenkung"]


# Die Logik 0.2.44: behielt bevorzugt die zum neuen Port passende Regel, auch wenn sie offen war.
_ALT_0244 = r'''
function Sync-RelayFirewall([int]$WebPort) {
    $smtpName = "EXO SMTP Relay - SMTP 25"
    $webName = "EXO SMTP Relay - Web $WebPort"
    $smtp = @(Get-NetFirewallRule -DisplayName $smtpName -ErrorAction SilentlyContinue)
    if ($smtp.Count -eq 0) {
        New-NetFirewallRule -DisplayName $smtpName -Direction Inbound -Protocol TCP -LocalPort 25 -Action Allow -Profile Any | Out-Null
    }
    $web = @(Get-NetFirewallRule -DisplayName "EXO SMTP Relay - Web *" -ErrorAction SilentlyContinue)
    if ($web.Count -eq 0) {
        New-NetFirewallRule -DisplayName $webName -Direction Inbound -Protocol TCP -LocalPort $WebPort -Action Allow -Profile Any | Out-Null
        return
    }
    $behalten = @($web | Where-Object { $_.DisplayName -eq $webName })
    if ($behalten.Count -gt 0) { $behalten = $behalten[0] } else { $behalten = $web[0] }
    foreach ($r in $web) { if ($r.Name -ne $behalten.Name) { Remove-NetFirewallRule -Name $r.Name } }
    $filter = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $behalten
    if ("$($filter.LocalPort)" -ne "$WebPort") { Set-NetFirewallPortFilter -InputObject $filter -LocalPort $WebPort }
    if ($behalten.DisplayName -ne $webName) { Set-NetFirewallRule -Name $behalten.Name -NewDisplayName $webName }
}
'''


@pytest.mark.skipif(not _ps(), reason="keine PowerShell (pwsh/powershell) vorhanden")
def test_firewall_gegenprobe_0244_waehlt_offene_regel(tmp_path):
    """Labtest 0.2.44: die zum Port passende OFFENE Regel gewann gegen die eingeschraenkte."""
    assert _pruefen(tmp_path, _ALT_0244) == ["eingeschraenkte_schlaegt_offene", "profil_zaehlt_als_einschraenkung"]


def test_firewall_statisch_kein_loeschen_und_neu_anlegen():
    funktion = _funktion()
    # Ausserhalb der Funktion keine Firewall-Cmdlets; der Firewall-Schritt ruft nur sie.
    rest = "\n".join(z for z in INSTALL.read_text(encoding="utf-8-sig").replace(funktion, "").splitlines()
                     if not z.lstrip().startswith("#"))
    for cmdlet in ("New-NetFirewallRule", "Remove-NetFirewallRule", "Set-NetFirewallRule"):
        assert cmdlet not in rest, cmdlet
    assert "Sync-RelayFirewall -WebPort $WebPort" in rest
    # Neu anlegen nur, wenn es keine Regel gibt; Entfernen nur für überzählige Web-Regeln.
    assert funktion.count("New-NetFirewallRule") == 2
    assert "if ($smtp.Count -eq 0)" in funktion and "if ($web.Count -eq 0)" in funktion
    assert "| Remove-NetFirewallRule" not in funktion
    assert "if ($r.Name -ne $behalten.Name)" in funktion
    assert "Set-NetFirewallPortFilter -InputObject $filter -LocalPort $WebPort" in funktion
    # Auswahl nach Einschraenkung, nicht nach Port (Labtest 0.2.44).
    assert "Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $r" in funktion
    assert "if ($stufe -gt $besteStufe)" in funktion
