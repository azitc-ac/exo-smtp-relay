"""Labtest 0.2.44 (Windows Server 2025): Rechte auf data\\ ERZWINGEN.

install.ps1 schaltete auf data\\ nur die Vererbung ab und FÜGTE SYSTEM und Administratoren
hinzu. Ein expliziter fremder Eintrag („BUILTIN\\Backup Operators: Read“) blieb stehen, die
Meldung „nur fuer SYSTEM und Administratoren lesbar“ stimmte nicht. Jetzt (Set-RelayDataAcl):
genau SYSTEM und Administratoren, jeder andere explizite Eintrag wird entfernt und gemeldet,
Unterobjekte erben wieder (eigene Einträge weg, Vererbung an) — ebenfalls gemeldet.

Labtest 0.2.45: `& icacls.exe … /reset /C /Q 2>&1` unter ErrorActionPreference=Stop — in PS 5.1
wird die stderr-Zeile („Access is denied“) zum beendenden Fehler, install.ps1 brach nach dem
Dienststopp ab. Und `/C` liefert Exit 0 trotz „Failed processing“, die WARNUNG kam nie. Der
frühere Nachbau ersetzte Reset-RelayAclErbe ganz und sah das nicht. Jetzt läuft die ECHTE
Funktion gegen ein nachgebautes icacls.exe (Exit-Codes wie gemessen), und der echte NTFS-Lauf
nimmt eine wirklich unzugängliche Datei (Eigentümer SYSTEM, nur SYSTEM:F).

Drei Ebenen: Nachbau von Get-Acl/Set-Acl/icacls (läuft mit pwsh auch unter Linux), ein echter
Lauf gegen NTFS (nur Windows) und ein statischer Test.
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
_HILFEN = ("New-RelayVollzugriff", "Get-RelayKontoName", "Reset-RelayAclErbe", "Set-RelayDataAcl",
           "Set-RelayDataAclKern")


def _ps() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _funktion(name: str) -> str:
    ps = INSTALL.read_text(encoding="utf-8-sig")
    m = re.search(rf"^function {re.escape(name)}\b.*?^\}}", ps, re.S | re.M)
    assert m, f"{name} fehlt in install.ps1"
    return m.group(0)


def _alle_funktionen() -> str:
    return "\n".join(_funktion(n) for n in _HILFEN) + "\n"


# Nachbau: ACL-Objekte mit den Methoden, die Set-RelayDataAcl benutzt. Get-Acl liefert eine
# KOPIE — wer Set-Acl vergisst, ändert nichts. Die Nachbauten der Hilfsfunktionen stehen NACH
# den echten und ersetzen sie (SecurityIdentifier gibt es unter Linux nicht).
_MOCKS = r'''
$global:Meldungen = New-Object System.Collections.ArrayList
$global:Acls = @{}
$global:Gesperrt = @()
$global:Zurueckgesetzt = New-Object System.Collections.ArrayList
function Write-Ok([string]$m) { [void]$global:Meldungen.Add("OK: $m") }
function Write-Warn([string]$m) { [void]$global:Meldungen.Add("WARN: $m") }
function Ace($sid, $rechte, $typ = "Allow", $inh = "ContainerInherit, ObjectInherit", $erbt = $false) {
    [pscustomobject]@{ IdentityReference = [pscustomobject]@{ Value = $sid }; FileSystemRights = $rechte;
        AccessControlType = $typ; InheritanceFlags = $inh; PropagationFlags = "None"; IsInherited = $erbt } }
function NeueAcl([bool]$geschuetzt, $regeln) {
    $o = [pscustomobject]@{ Regeln = (New-Object System.Collections.ArrayList); Geschuetzt = $geschuetzt }
    foreach ($r in @($regeln)) { if ($r) { [void]$o.Regeln.Add($r) } }
    $o | Add-Member ScriptProperty AreAccessRulesProtected { $this.Geschuetzt }
    $o | Add-Member ScriptMethod GetAccessRules { param($e, $i, $t)
        $aus = @(); if ($e) { $aus += @($this.Regeln | Where-Object { -not $_.IsInherited }) }
        if ($i) { $aus += @($this.Regeln | Where-Object { $_.IsInherited }) }; , $aus }
    $o | Add-Member ScriptMethod RemoveAccessRuleSpecific { param($r) $this.Regeln.Remove($r) }
    $o | Add-Member ScriptMethod AddAccessRule { param($r) [void]$this.Regeln.Add($r) }
    $o | Add-Member ScriptMethod SetAccessRuleProtection { param($p, $behalten)
        $this.Geschuetzt = $p
        if ($p -and -not $behalten) { foreach ($r in @($this.Regeln | Where-Object { $_.IsInherited })) { $this.Regeln.Remove($r) } } }
    $o }
function Objekt($pfad, [bool]$geschuetzt, $regeln) { $global:Acls[$pfad] = NeueAcl $geschuetzt $regeln }
function Get-Acl { [CmdletBinding()] param([Parameter(Position = 0)][string]$LiteralPath)
    $a = $global:Acls[$LiteralPath]; NeueAcl $a.Geschuetzt $a.Regeln }
function Set-Acl { [CmdletBinding()] param([Parameter(Position = 0)][string]$LiteralPath, [Parameter(Position = 1)]$AclObject)
    $global:Acls[$LiteralPath] = $AclObject }
function Get-ChildItem { [CmdletBinding()] param([string]$LiteralPath, [switch]$Recurse, [switch]$Force)
    foreach ($k in @($global:Acls.Keys | Sort-Object)) { if ($k.StartsWith("$LiteralPath\")) { [pscustomobject]@{ FullName = $k } } } }
'''

# Nachbau der Hilfen, die SIDs brauchen (gibt es unter Linux nicht). Reset-RelayAclErbe bleibt
# ECHT; nachgebaut ist nur icacls.exe — mit den im Labtest gemessenen Exit-Codes: gesperrtes
# Objekt → stderr „Access is denied“, Exit 5; mit /C trotzdem Exit 0.
_MOCK_HILFEN = r'''
$global:Namen = @{ "S-1-5-18" = "NT AUTHORITY\SYSTEM"; "S-1-5-32-544" = "BUILTIN\Administratoren";
    "S-1-5-32-551" = "BUILTIN\Backup Operators"; "S-1-5-32-545" = "BUILTIN\Benutzer"; "S-1-1-0" = "Jeder" }
function New-RelayVollzugriff([string]$Sid) { Ace $Sid "FullControl" }
function Get-RelayKontoName($Identitaet) { $global:Namen[$Identitaet.Value] }
$global:IcaclsAufrufe = New-Object System.Collections.ArrayList
function icacls.exe {
    $pfad = "$($args[0])"
    [void]$global:IcaclsAufrufe.Add(($args -join " "))
    if ($global:Gesperrt -contains $pfad) {
        $global:LASTEXITCODE = $(if ($args -contains "/C") { 0 } else { 5 })
        Write-Error "${pfad}: Access is denied."
        "Successfully processed 0 files; Failed processing 1 files"
        return
    }
    [void]$global:Zurueckgesetzt.Add($pfad)
    $a = $global:Acls[$pfad]
    foreach ($r in @($a.Regeln | Where-Object { -not $_.IsInherited })) { $a.Regeln.Remove($r) }
    $a.Geschuetzt = $false
    "processed file: $pfad"
    "Successfully processed 1 files; Failed processing 0 files"
    $global:LASTEXITCODE = 0
}
'''

_GEERBT = 'Ace "S-1-5-32-545" "ReadAndExecute" "Allow" "ContainerInherit, ObjectInherit" $true'
_SOLL = '(Ace "S-1-5-18" "FullControl"), (Ace "S-1-5-32-544" "FullControl")'

_BESTAND = {
    # Labtest: Backup Operators Read explizit auf data\, Vererbung noch an; dazu Unterobjekte mit
    # eigenem Eintrag bzw. abgeschalteter Vererbung und eins, das schon korrekt nur erbt.
    "fremd": (f'Objekt "C:\\d" $false @((Ace "S-1-5-18" "FullControl"), (Ace "S-1-5-32-544" "FullControl"), '
              f'(Ace "S-1-5-32-551" "Read"), ({_GEERBT}))\n'
              'Objekt "C:\\d\\logs" $true @((Ace "S-1-5-32-545" "Modify"))\n'
              'Objekt "C:\\d\\logs\\app.log" $false @()\n'
              'Objekt "C:\\d\\settings.json" $false @((Ace "S-1-1-0" "Read" "Allow" "None"))\n'),
    "sauber": (f'Objekt "C:\\d" $true @({_SOLL})\n'
               'Objekt "C:\\d\\logs" $false @()\n'),
    # data\ selbst nicht lesbar (Get-Acl wirft): der Schritt darf nie beendend sein.
    "wurzel_kaputt": 'Objekt "C:\\anders" $false @()\n',
    "gesperrt": (f'Objekt "C:\\d" $true @({_SOLL})\n'
                 'Objekt "C:\\d\\alt" $true @((Ace "S-1-5-32-545" "Modify"))\n'
                 '$global:Gesperrt = @("C:\\d\\alt")\n'),
}

_AUSGABE = r'''
$zustand = [ordered]@{}
foreach ($k in @($global:Acls.Keys | Sort-Object)) {
    $a = $global:Acls[$k]
    $zustand[$k] = [ordered]@{ Geschuetzt = [bool]$a.Geschuetzt
        Explizit = @($a.Regeln | Where-Object { -not $_.IsInherited } | ForEach-Object { "$($_.IdentityReference.Value) $($_.AccessControlType) $($_.FileSystemRights)" } | Sort-Object)
        Geerbt = @($a.Regeln | Where-Object { $_.IsInherited }).Count }
}
ConvertTo-Json -Compress -Depth 5 -InputObject ([ordered]@{ Acls = $zustand; Meldungen = @($global:Meldungen); Zurueck = @($global:Zurueckgesetzt); Icacls = @($global:IcaclsAufrufe) })
'''

# Die Logik bis 0.2.44 — nur für die eingebaute Gegenprobe (Konten als SID wie im Nachbau).
_ALT = r'''
function Set-RelayDataAcl([string]$Pfad) {
    $acl = Get-Acl $Pfad
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($wer in @("S-1-5-18", "S-1-5-32-544")) { $acl.AddAccessRule((New-RelayVollzugriff $wer)) }
    Set-Acl $Pfad $acl
    Write-Ok "data\ nur fuer SYSTEM und Administratoren lesbar"
}
'''


def _lauf(tmp_path, funktionen: str, bestand: str) -> dict:
    skript = tmp_path / "acl.ps1"
    skript.write_text("Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
                      + funktionen + _MOCKS + _MOCK_HILFEN + _BESTAND[bestand]
                      + 'Set-RelayDataAcl -Pfad "C:\\d"\n' + _AUSGABE, encoding="utf-8-sig")
    r = subprocess.run([_ps(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(skript)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(r.stdout.strip().splitlines()[-1])


_SOLL_WURZEL = {"Geschuetzt": True, "Explizit": ["S-1-5-18 Allow FullControl", "S-1-5-32-544 Allow FullControl"],
                "Geerbt": 0}
_NUR_GEERBT = {"Geschuetzt": False, "Explizit": [], "Geerbt": 0}


def _pruefen(tmp_path, funktionen: str) -> list[str]:
    """Alle Erwartungen; liefert die Namen der verletzten (leer = alles gut)."""
    fehler = []
    e = _lauf(tmp_path, funktionen, "fremd")
    if e["Acls"]["C:\\d"] != _SOLL_WURZEL:
        fehler.append("fremd_wurzel")
    if any(e["Acls"][k] != _NUR_GEERBT for k in ("C:\\d\\logs", "C:\\d\\settings.json", "C:\\d\\logs\\app.log")):
        fehler.append("fremd_unterobjekte")
    if e["Meldungen"] != [
            "OK: entfernt: BUILTIN\\Backup Operators Allow Read",
            "OK: entfernt auf logs: BUILTIN\\Benutzer Allow Modify",
            "OK: Vererbung wieder an: logs",
            "OK: entfernt auf settings.json: Jeder Allow Read",
            "OK: data\\ nur fuer SYSTEM und Administratoren lesbar (1 fremde Eintraege entfernt, 2 Unterobjekte zurueckgesetzt)"]:
        fehler.append("fremd_meldungen")
    if e.get("Zurueck") != ["C:\\d\\logs", "C:\\d\\settings.json"]:
        fehler.append("nur_abweichende_anfassen")
    e = _lauf(tmp_path, funktionen, "sauber")
    if e["Acls"]["C:\\d"] != _SOLL_WURZEL or e["Meldungen"] != [
            "OK: data\\ nur fuer SYSTEM und Administratoren lesbar (0 fremde Eintraege entfernt, 0 Unterobjekte zurueckgesetzt)"]:
        fehler.append("sauber")
    e = _lauf(tmp_path, funktionen, "gesperrt")
    if not e["Meldungen"] or not e["Meldungen"][-1].startswith("WARN: ") or \
            not any("nicht zurueckgesetzt: alt" in m for m in e["Meldungen"]):
        fehler.append("gesperrt_warnt")
    # Der ECHTE Exit-Code und die icacls-Meldung stehen in der Warnung (EAP lokal Continue —
    # sonst bricht PS 5.1 an der stderr-Zeile ab, bevor der Exit-Code gelesen ist).
    if not any("nicht zurueckgesetzt: alt (Exit 5: " in m and "Access is denied" in m for m in e["Meldungen"]):
        fehler.append("gesperrt_exitcode")
    if any(" /C" in a for a in e.get("Icacls") or []):
        fehler.append("icacls_ohne_C")
    return fehler


@pytest.mark.skipif(not _ps(), reason="keine PowerShell (pwsh/powershell) vorhanden")
def test_data_acl_wird_erzwungen_und_gemeldet(tmp_path):
    assert _pruefen(tmp_path, _alle_funktionen()) == []


@pytest.mark.skipif(not _ps(), reason="keine PowerShell (pwsh/powershell) vorhanden")
def test_data_acl_gegenprobe_alte_logik_faellt_durch(tmp_path):
    """Trennscharf: die Logik bis 0.2.44 lässt den fremden Eintrag stehen und meldet trotzdem Erfolg."""
    assert _pruefen(tmp_path, _ALT) == ["fremd_wurzel", "fremd_unterobjekte", "fremd_meldungen",
                                        "nur_abweichende_anfassen", "sauber", "gesperrt_warnt",
                                        "gesperrt_exitcode"]


# Reset-RelayAclErbe wie in 0.2.45 (Labtest: Abbruch an „Access is denied“, /C verschluckt den Fehler).
_RESET_0245 = r'''
function Reset-RelayAclErbe([string]$Pfad) {
    $null = & icacls.exe $Pfad /reset /C /Q 2>&1
    return ($LASTEXITCODE -eq 0)
}
'''


def _mit_reset(reset: str) -> str:
    return _alle_funktionen().replace(_funktion("Reset-RelayAclErbe"), reset)


def _ohne_zeile(text: str, zeile: str) -> str:
    assert zeile in text, zeile
    return text.replace(zeile, "")


@pytest.mark.skipif(not _ps(), reason="keine PowerShell (pwsh/powershell) vorhanden")
@pytest.mark.parametrize("variante, erwartet", [
    ("0.2.45", ["gesperrt_warnt", "gesperrt_exitcode", "icacls_ohne_C"]),
    ("nur /C zurueck", ["gesperrt_warnt", "gesperrt_exitcode", "icacls_ohne_C"]),
    ("nur EAP zurueck", ["gesperrt_exitcode"]),
])
def test_reset_gegenprobe(tmp_path, variante, erwartet):
    """Trennscharf: jede Rücknahme des Fixes fällt durch."""
    echt = _funktion("Reset-RelayAclErbe")
    if variante == "0.2.45":
        reset = _RESET_0245
    elif variante == "nur /C zurueck":
        reset = echt.replace("/reset /Q", "/reset /C /Q")
    else:
        reset = _ohne_zeile(echt, '    $ErrorActionPreference = "Continue"\n')
    assert _pruefen(tmp_path, _mit_reset(reset)) == erwartet


@pytest.mark.skipif(sys.platform != "win32", reason="echte NTFS-Rechte nur unter Windows")
def test_data_acl_echt_auf_ntfs(tmp_path):
    """Echter Lauf: icacls legt fremde Einträge an, Set-RelayDataAcl muss sie wegräumen."""
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "app.log").write_text("x")
    (data / "settings.json").write_text("{}")

    def icacls(*args):
        r = subprocess.run(["icacls", *map(str, args)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr

    icacls(data, "/grant", "*S-1-5-32-551:(OI)(CI)(R)")            # Labtest: Backup Operators Read
    icacls(data / "logs", "/inheritance:d")
    icacls(data / "logs", "/grant", "*S-1-5-32-545:(OI)(CI)(M)")
    icacls(data / "settings.json", "/grant", "*S-1-1-0:(R)")
    ps = shutil.which("powershell") or _ps()                       # Ziel ist Windows PowerShell 5.1
    skript = tmp_path / "echt.ps1"
    skript.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        "$global:M = New-Object System.Collections.ArrayList\n"
        "function Write-Ok([string]$m) { [void]$global:M.Add(\"OK: $m\") }\n"
        "function Write-Warn([string]$m) { [void]$global:M.Add(\"WARN: $m\") }\n"
        + _alle_funktionen()
        + f"Set-RelayDataAcl -Pfad '{data}'\n"
        "$sid = [System.Security.Principal.SecurityIdentifier]\n"
        "$z = [ordered]@{}\n"
        f"foreach ($p in @('{data}') + @(Get-ChildItem -LiteralPath '{data}' -Recurse -Force | ForEach-Object {{ $_.FullName }})) {{\n"
        "  $a = Get-Acl -LiteralPath $p\n"
        "  $z[$p] = [ordered]@{ Geschuetzt = [bool]$a.AreAccessRulesProtected\n"
        "    Explizit = @($a.GetAccessRules($true, $false, $sid) | ForEach-Object { \"$($_.IdentityReference.Value) $($_.AccessControlType) $($_.FileSystemRights)\" } | Sort-Object) }\n"
        "}\n"
        "ConvertTo-Json -Compress -Depth 5 -InputObject ([ordered]@{ Acls = $z; Meldungen = @($global:M) })\n",
        encoding="utf-8-sig")
    # PSModulePath aus einer pwsh-Sitzung (CI-Schritt) laedt sonst die PS7-Module in 5.1:
    # "Get-Acl ... module could not be loaded".
    umgebung = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(skript)],
                       capture_output=True, text=True, timeout=120, env=umgebung)
    assert r.returncode == 0, r.stderr + r.stdout
    e = json.loads(r.stdout.strip().splitlines()[-1])
    acls = e["Acls"]
    assert acls[str(data)] == {"Geschuetzt": True,
                               "Explizit": ["S-1-5-18 Allow FullControl", "S-1-5-32-544 Allow FullControl"]}
    for pfad, wert in acls.items():
        if pfad != str(data):
            assert wert == {"Geschuetzt": False, "Explizit": []}, pfad
    meldungen = e["Meldungen"]
    assert any(m.startswith("OK: entfernt: ") and "Read" in m for m in meldungen), meldungen
    assert any(m.startswith("OK: Vererbung wieder an: ") and m.endswith("logs") for m in meldungen), meldungen
    assert meldungen[-1].startswith("OK: data\\ nur fuer SYSTEM und Administratoren lesbar"), meldungen


def test_data_acl_statisch():
    code = INSTALL.read_text(encoding="utf-8-sig")
    huelle = _funktion("Set-RelayDataAcl")
    funktion = _funktion("Set-RelayDataAclKern")
    rest = code
    for n in _HILFEN:
        rest = rest.replace(_funktion(n), "")
    rest = "\n".join(z for z in rest.splitlines() if not z.lstrip().startswith("#"))
    # Der Installer setzt Rechte NUR über Set-RelayDataAcl — und zwar vor dem Dienststart.
    assert "Set-RelayDataAcl -Pfad $dataDir" in rest
    for verboten in ("Set-Acl", "SetAccessRuleProtection", "icacls"):
        assert verboten not in rest, verboten
    assert rest.index("Set-RelayDataAcl -Pfad $dataDir") < rest.index('Write-Step "Starte Dienst"')
    # Wurzel: Vererbung ab, alle expliziten Einträge raus, genau die zwei SIDs rein.
    assert "$acl.SetAccessRuleProtection($true, $false)" in funktion
    assert "[void]$acl.RemoveAccessRuleSpecific($r)" in funktion
    assert '$soll = @("S-1-5-18", "S-1-5-32-544")' in funktion
    # Unterobjekte: rekursiv, Rücksetzen per icacls /reset (Eigentümer bleibt).
    assert "Get-ChildItem -LiteralPath $Pfad -Recurse -Force" in funktion
    reset = "\n".join(z for z in _funktion("Reset-RelayAclErbe").splitlines() if not z.lstrip().startswith("#"))
    assert "/reset /Q" in reset and "/C" not in reset
    # Nie beendend: der Kern läuft in try/catch, ein Fehler wird zur WARNUNG.
    assert "try {\n        Set-RelayDataAclKern -Pfad $Pfad" in huelle
    # Konten per SID, nie per (sprachabhaengigem) Namen.
    ohne_kommentare = "\n".join(z for z in code.splitlines() if not z.lstrip().startswith("#"))
    assert "BUILTIN\\Administrators" not in ohne_kommentare and "NT AUTHORITY\\SYSTEM" not in ohne_kommentare


def _ps51_lauf(skript) -> dict:
    """Skript unter Windows PowerShell 5.1; Ergebnis-JSON aus <skript>.json."""
    ps = shutil.which("powershell") or _ps()
    aus = skript.with_suffix(".json")
    umgebung = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(skript), str(aus)],
                       capture_output=True, text=True, timeout=120, env=umgebung)
    assert r.returncode == 0 and aus.is_file(), r.stderr + r.stdout
    return json.loads(aus.read_text(encoding="utf-8-sig"))


_C2_KOPF = ("param([string]$Aus)\nSet-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
            "$global:M = New-Object System.Collections.ArrayList\n"
            "function Write-Ok([string]$m) { [void]$global:M.Add(\"OK: $m\") }\n"
            "function Write-Warn([string]$m) { [void]$global:M.Add(\"WARN: $m\") }\n")


@pytest.mark.skipif(sys.platform != "win32", reason="echte NTFS-Rechte nur unter Windows")
def test_reset_echt_icacls_fehler_unter_ps51(tmp_path):
    """Kern des Labtest-Fehlers ohne Sonderrechte: ECHTES icacls scheitert mit stderr und
    Exit ≠ 0 (hier: Pfad gibt es nicht). Unter PS 5.1 mit EAP=Stop brach 0.2.45 daran ab;
    jetzt False, und die Warnung trägt den ECHTEN Exit-Code (nicht -1)."""
    skript = tmp_path / "fehlt.ps1"
    skript.write_text(_C2_KOPF + _alle_funktionen()
                      + f"$d = Reset-RelayAclErbe '{tmp_path / 'gibt-es-nicht' / 'x.json'}'\n"
                      "ConvertTo-Json -Compress -InputObject ([ordered]@{ Direkt = $d; Meldung = $script:RelayIcaclsMeldung }) | Set-Content -Encoding UTF8 $Aus\n",
                      encoding="utf-8-sig")
    e = _ps51_lauf(skript)
    assert e["Direkt"] is False, e
    assert re.match(r"^Exit [1-9]\d*: ", e["Meldung"]), e


@pytest.mark.skipif(sys.platform != "win32", reason="echte NTFS-Rechte nur unter Windows")
def test_reset_echt_gegenprobe_ohne_eap(tmp_path):
    """Trennscharf auf echtem PS 5.1: ohne die lokale EAP-Absicherung wird die stderr-Zeile zum
    beendenden Fehler — der Exit-Code ist dann nicht mehr zu lesen („Exit -1“)."""
    ohne = _ohne_zeile(_funktion("Reset-RelayAclErbe"), '    $ErrorActionPreference = "Continue"\n')
    skript = tmp_path / "ohne_eap.ps1"
    skript.write_text(_C2_KOPF + _mit_reset(ohne)
                      + f"$d = Reset-RelayAclErbe '{tmp_path / 'gibt-es-nicht' / 'x.json'}'\n"
                      "ConvertTo-Json -Compress -InputObject ([ordered]@{ Direkt = $d; Meldung = $script:RelayIcaclsMeldung }) | Set-Content -Encoding UTF8 $Aus\n",
                      encoding="utf-8-sig")
    e = _ps51_lauf(skript)
    assert e["Meldung"].startswith("Exit -1"), e


@pytest.mark.skipif(sys.platform != "win32", reason="echte NTFS-Rechte nur unter Windows")
def test_data_acl_echt_unzugaengliche_datei(tmp_path):
    """Labtest 0.2.45, Fall c2: Datei mit Eigentümer SYSTEM und nur SYSTEM:F. Im Temp-Verzeichnis
    des CI-Runners darf der Administrator sie trotzdem zurücksetzen (gemessen 25.11.2026; dort
    gehört ihm der Ordner) — dieser Test prüft dann nur: kein Abbruch, Exit 0; scheitert icacls,
    mit echtem Exit-Code und WARNUNG. Den echten Fall in data\ (Exit 5, „Access is denied“)
    prüft der CI-Job „Installer echt“ (tests/installer_echt.ps1), den Fehlerweg ohne
    Sonderrechte test_reset_echt_icacls_fehler_unter_ps51 samt Gegenprobe ohne EAP."""
    data = tmp_path / "data"
    data.mkdir()
    gesperrt = data / "gesperrt.json"
    gesperrt.write_text("{}")

    def icacls(*args):
        r = subprocess.run(["icacls", *map(str, args)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr

    icacls(gesperrt, "/setowner", "*S-1-5-18")                    # solange wir noch erben: Vollzugriff
    icacls(gesperrt, "/inheritance:r", "/grant:r", "*S-1-5-18:F")
    try:
        skript = tmp_path / "c2.ps1"
        skript.write_text(_C2_KOPF + _alle_funktionen()
                          + f"$d = Reset-RelayAclErbe '{gesperrt}'\n$dm = $script:RelayIcaclsMeldung\n"
                          f"Set-RelayDataAcl -Pfad '{data}'\n"
                          "ConvertTo-Json -Compress -InputObject ([ordered]@{ Direkt = $d; DirektMeldung = $dm;"
                          " Meldungen = @($global:M) }) | Set-Content -Encoding UTF8 $Aus\n",
                          encoding="utf-8-sig")
        e = _ps51_lauf(skript)                                     # Exit 0 prüft _ps51_lauf
        if e["Direkt"] is False:
            assert re.match(r"^Exit [1-9]\d*: ", e["DirektMeldung"]), f"Exit-Code nicht gelesen (EAP?): {e}"
            assert any(m.startswith("WARN: ") and "gesperrt.json" in m for m in e["Meldungen"]), e
        else:
            assert e["DirektMeldung"] == "", e
    finally:
        subprocess.run(["takeown", "/f", str(gesperrt)], capture_output=True)
        subprocess.run(["icacls", str(gesperrt), "/reset"], capture_output=True)


@pytest.mark.skipif(not _ps(), reason="keine PowerShell (pwsh/powershell) vorhanden")
def test_data_acl_nie_beendend(tmp_path):
    """Labtest 0.2.45: Ein Fehler im Rechte-Schritt brach install.ps1 nach dem Dienststopp ab.
    Jetzt WARNUNG, das Skript läuft weiter (Exit 0 prüft _lauf)."""
    e = _lauf(tmp_path, _alle_funktionen(), "wurzel_kaputt")
    assert e["Meldungen"] and e["Meldungen"][-1].startswith("WARN: Rechte auf data\\ nicht vollstaendig gesetzt: "), e
