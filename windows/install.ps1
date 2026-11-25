<#
.SYNOPSIS
    Installiert das EXO SMTP Relay als Windows-Dienst.

.DESCRIPTION
    Legt das Installationsverzeichnis an (Vorgabe C:\ProgramData\exo-smtp-relay),
    kopiert die Anwendung, erstellt eine Python-venv, installiert die
    Abhaengigkeiten aus requirements.lock, registriert den Dienst "ExoSmtpRelay"
    und oeffnet die Firewall fuer Port 25 und den Web-Port.

    Voraussetzungen:
      * Windows Server 2016+ oder Windows 10/11, als Administrator ausfuehren
      * Python 3.11 bis 3.13 (https://www.python.org/downloads/windows/,
        "Add python.exe to PATH" anhaken) - oder -PythonExe angeben
      * Fuer die Postfachabfrage: ExchangeOnlineManagement (der Installer bietet die
        festgelegte, gepruefte Fassung an; im Offline-Paket liegt sie bei)

    Laeuft unter Windows PowerShell 5.1 und PowerShell 7.

.PARAMETER InstallDir
    Zielverzeichnis. Vorgabe: C:\ProgramData\exo-smtp-relay

.PARAMETER PythonExe
    Pfad zu python.exe (3.11 bis 3.13). Ohne Angabe: "py -3.13", "-3.12", "-3.11", "py -3", "python".

.PARAMETER WebPort
    Port der Weboberflaeche. Vorgabe: 8443 bei der Erstinstallation; bei einem Upgrade
    ohne Angabe bleibt der bisherige Port.

.PARAMETER SkipFirewall
    Keine Firewall-Regeln anlegen.

.PARAMETER Unattended
    Keine Rueckfragen: Python und ExchangeOnlineManagement werden ohne Nachfrage installiert.

.PARAMETER SkipExoModule
    ExchangeOnlineManagement nicht installieren/pruefen.

.NOTES
    Lieferkette: Python-Installer und ExchangeOnlineManagement kommen von fest eingetragenen
    Adressen mit fester SHA256 (siehe $RelayPythonInstaller / $RelayExoModul) und werden vor
    Gebrauch auf Pruefsumme und Authenticode-Signatur geprueft; Python-Pakete nur mit
    pip --require-hashes. Offline-Paket (exo-smtp-relay-<version>-windows-offline.zip): bringt
    alles mit, installiert OHNE Internet und meldet "Offline-Installation".

.EXAMPLE
    .\install.ps1
.EXAMPLE
    .\install.ps1 -InstallDir "D:\Dienste\exo-smtp-relay" -WebPort 8443
#>
param(
    [string]$InstallDir = "C:\ProgramData\exo-smtp-relay",
    [string]$PythonExe = "",
    [int]$WebPort = 8443,
    [switch]$SkipFirewall,
    [switch]$SkipExoModule,
    [switch]$Unattended
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "    OK: $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "    WARNUNG: $msg" -ForegroundColor Yellow }

# Firewall-Regeln des Relays. Erstinstallation: anlegen (alle Profile). Upgrade: bestehende Regeln
# NIE loeschen und neu anlegen - eigene Einschraenkungen (RemoteAddress, Profile, Enabled) blieben
# sonst auf der Strecke und das Relay waere danach weiter offen als vorher. Angepasst wird nur der
# Port; nach einem Portwechsel bleibt genau eine Web-Regel. Getestet in tests\test_windows_upgrade_meldungen.py.
function Sync-RelayFirewall([int]$WebPort) {
    $smtpName = "EXO SMTP Relay - SMTP 25"
    $webName = "EXO SMTP Relay - Web $WebPort"
    $smtp = @(Get-NetFirewallRule -DisplayName $smtpName -ErrorAction SilentlyContinue)
    if ($smtp.Count -eq 0) {
        New-NetFirewallRule -DisplayName $smtpName -Direction Inbound -Protocol TCP `
            -LocalPort 25 -Action Allow -Profile Any | Out-Null
        Write-Ok "Port 25 eingehend (alle Netzwerkprofile)"
    } else {
        Write-Ok "Regel '$smtpName' vorhanden - Einschraenkungen unveraendert"
    }
    $web = @(Get-NetFirewallRule -DisplayName "EXO SMTP Relay - Web *" -ErrorAction SilentlyContinue)
    if ($web.Count -eq 0) {
        New-NetFirewallRule -DisplayName $webName -Direction Inbound -Protocol TCP `
            -LocalPort $WebPort -Action Allow -Profile Any | Out-Null
        Write-Ok "Port $WebPort eingehend (alle Netzwerkprofile)"
        return
    }
    # Eine Regel behalten, weitere Web-Regeln entfernen. Nie weiter oeffnen als vorher: es bleibt die
    # am staerksten eingeschraenkte (RemoteAddress, Profil), erst bei Gleichstand die zum Port passende,
    # sonst die erste. Eine offene Any-Regel gewinnt nie gegen eine eingeschraenkte (Labtest 0.2.44).
    $behalten = $null
    $besteStufe = -1
    foreach ($r in $web) {
        $stufe = 0
        $adressen = "$((Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $r).RemoteAddress)"
        if ($adressen -ne "Any") { $stufe += 2 }
        $profil = "$($r.Profile)"
        $alleProfile = ($profil -match "Domain") -and ($profil -match "Private") -and ($profil -match "Public")
        if ($profil -ne "Any" -and -not $alleProfile) { $stufe += 2 }
        if ($r.DisplayName -eq $webName) { $stufe += 1 }
        if ($stufe -gt $besteStufe) { $behalten = $r; $besteStufe = $stufe }
    }
    foreach ($r in $web) {
        if ($r.Name -ne $behalten.Name) {
            Remove-NetFirewallRule -Name $r.Name
            Write-Ok "Alte Regel '$($r.DisplayName)' entfernt"
        }
    }
    $filter = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $behalten
    if ("$($filter.LocalPort)" -ne "$WebPort") {
        Set-NetFirewallPortFilter -InputObject $filter -LocalPort $WebPort
    }
    if ($behalten.DisplayName -ne $webName) {
        Set-NetFirewallRule -Name $behalten.Name -NewDisplayName $webName
    }
    Write-Ok "Regel '$webName' auf Port $WebPort - Einschraenkungen unveraendert"
}

# Rechte auf data\ ERZWINGEN (Labtest 0.2.44): genau SYSTEM (S-1-5-18) und Administratoren
# (S-1-5-32-544) mit Vollzugriff, vererbt auf Unterordner und Dateien. Jeder andere explizite
# Eintrag auf data\ wird entfernt und gemeldet; Unterobjekte mit eigenen Eintraegen oder
# abgeschalteter Vererbung werden auf "nur geerbt" zurueckgesetzt und ebenfalls gemeldet.
# SIDs statt Namen: Kontonamen sind sprachabhaengig (deutsches Windows: "VORDEFINIERT\Administratoren").
# Der Dienst laeuft als LocalSystem und behaelt Zugriff. Getestet in tests\test_windows_data_acl.py.
function New-RelayVollzugriff([string]$Sid) {
    New-Object System.Security.AccessControl.FileSystemAccessRule(
        (New-Object System.Security.Principal.SecurityIdentifier($Sid)), "FullControl",
        "ContainerInherit,ObjectInherit", "None", "Allow")
}

function Get-RelayKontoName($Identitaet) {
    try { return $Identitaet.Translate([System.Security.Principal.NTAccount]).Value } catch { return "$($Identitaet.Value)" }
}

function Reset-RelayAclErbe([string]$Pfad) {
    # icacls /reset: nur geerbte Rechte, Vererbung an; Eigentuemer bleibt unberuehrt.
    # Labtest 0.2.45 (Datei: Eigentuemer SYSTEM, nur SYSTEM:F): Unter PS 5.1 wird mit
    # ErrorActionPreference=Stop jede stderr-Zeile eines nativen Programms bei 2>&1 zum
    # beendenden Fehler - install.ps1 brach ab, der Dienst blieb gestoppt. Deshalb EAP hier
    # lokal Continue. Und KEIN /C: damit meldet icacls Exit 0 trotz "Failed processing".
    # Getestet in tests\test_windows_data_acl.py (Nachbau und echter NTFS-Lauf).
    $script:RelayIcaclsMeldung = ""
    $vorher = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = -1
    try {
        $ausgabe = @(& icacls.exe $Pfad /reset /Q 2>&1 | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
        $rc = $LASTEXITCODE
    } catch {
        $ausgabe = @("$($_.Exception.Message)")
        $rc = -1
    } finally {
        $ErrorActionPreference = $vorher
    }
    if ($rc -eq 0) { return $true }
    $script:RelayIcaclsMeldung = "Exit $rc$(if ($ausgabe.Count) { ': ' + ($ausgabe -join ' ') })"
    return $false
}

function Set-RelayDataAcl([string]$Pfad) {
    # Nie beendend (Labtest 0.2.45): Ein Fehler hier darf das Upgrade nicht abbrechen -
    # er wird als WARNUNG gemeldet, der Installer laeuft weiter und startet den Dienst.
    try {
        Set-RelayDataAclKern -Pfad $Pfad
    } catch {
        Write-Warn "Rechte auf data\ nicht vollstaendig gesetzt: $($_.Exception.Message) - bitte pruefen: icacls `"$Pfad`""
    }
}

function Set-RelayDataAclKern([string]$Pfad) {
    $script:RelayIcaclsMeldung = ""
    $soll = @("S-1-5-18", "S-1-5-32-544")
    $sidTyp = [System.Security.Principal.SecurityIdentifier]
    $probleme = 0
    $entfernt = 0
    $acl = Get-Acl -LiteralPath $Pfad
    foreach ($r in @($acl.GetAccessRules($true, $false, $sidTyp))) {
        $passt = ($soll -contains "$($r.IdentityReference.Value)") -and ("$($r.AccessControlType)" -eq "Allow") -and
                 ("$($r.FileSystemRights)" -eq "FullControl") -and
                 ("$($r.InheritanceFlags)" -eq "ContainerInherit, ObjectInherit") -and ("$($r.PropagationFlags)" -eq "None")
        if (-not $passt) {
            Write-Ok "entfernt: $(Get-RelayKontoName $r.IdentityReference) $($r.AccessControlType) $($r.FileSystemRights)"
            $entfernt++
        }
        [void]$acl.RemoveAccessRuleSpecific($r)
    }
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in $soll) { $acl.AddAccessRule((New-RelayVollzugriff $sid)) }
    Set-Acl -LiteralPath $Pfad -AclObject $acl

    # Unterobjekte: nur geerbte Rechte. Nur anfassen, was abweicht - und jedes melden.
    $zurueck = 0
    $fehler = @()
    $objekte = @(Get-ChildItem -LiteralPath $Pfad -Recurse -Force -ErrorAction SilentlyContinue -ErrorVariable fehler)
    foreach ($f in $fehler) { Write-Warn "nicht lesbar: $($f.TargetObject)"; $probleme++ }
    foreach ($o in $objekte) {
        $name = $o.FullName
        if ($name.StartsWith($Pfad, [System.StringComparison]::OrdinalIgnoreCase)) { $name = $name.Substring($Pfad.Length).TrimStart("\", "/") }
        try {
            $a = Get-Acl -LiteralPath $o.FullName
            $eigene = @($a.GetAccessRules($true, $false, $sidTyp))
            $geschuetzt = [bool]$a.AreAccessRulesProtected
        } catch {
            Write-Warn "Rechte nicht lesbar: $name"; $probleme++; continue
        }
        if ($eigene.Count -eq 0 -and -not $geschuetzt) { continue }
        # Meldungen erst NACH dem Zuruecksetzen (Labtest 0.2.46: "entfernt auf ..." stand auch
        # vor einem gescheiterten Reset).
        if (Reset-RelayAclErbe $o.FullName) {
            foreach ($r in $eigene) { Write-Ok "entfernt auf ${name}: $(Get-RelayKontoName $r.IdentityReference) $($r.AccessControlType) $($r.FileSystemRights)" }
            if ($geschuetzt) { Write-Ok "Vererbung wieder an: $name" }
            $zurueck++
        } else { Write-Warn "nicht zurueckgesetzt: $name ($($script:RelayIcaclsMeldung); von Hand: icacls `"$($o.FullName)`" /reset)"; $probleme++ }
    }
    if ($probleme -eq 0) {
        Write-Ok "data\ nur fuer SYSTEM und Administratoren lesbar ($entfernt fremde Eintraege entfernt, $zurueck Unterobjekte zurueckgesetzt)"
    } else {
        Write-Warn "data\ auf SYSTEM und Administratoren gesetzt, aber $probleme Unterobjekt(e) weichen ab - siehe oben"
    }
}

# Was install.ps1 aus dem Paket nach InstallDir kopiert - und beim Upgrade vorher nach
# .install-alt\ sichert. data\ und venv\ stehen nie darin.
$RelayTeile = @("app", "windows", "VERSION", "CHANGELOG.md", "LICENSE.md", "NOTICE", "README.md", "offline")

# Natives Programm aufrufen und den Exit-Code PRUEFEN (Labtest 0.2.45: venv, pip und service.py
# meldeten nach einem Fehler trotzdem "OK"). EAP lokal Continue: unter PS 5.1 wird stderr bei 2>&1
# sonst zum beendenden Fehler, bevor der Exit-Code ausgewertet ist. Rueckgabe: stdout-Zeilen.
function Invoke-RelayNativ([string]$Was, [scriptblock]$Befehl) {
    $vorher = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = -1
    try {
        $roh = @(& $Befehl 2>&1)
        $rc = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $vorher
    }
    $fehlerTyp = [System.Management.Automation.ErrorRecord]
    $aus = @($roh | Where-Object { $_ -isnot $fehlerTyp } | ForEach-Object { "$_" })
    if ($rc -ne 0) {
        $letzte = @($roh | ForEach-Object { "$_".Trim() } | Where-Object { $_ } | Select-Object -Last 12)
        throw "$Was fehlgeschlagen (Exit $rc): $($letzte -join ' | ')"
    }
    $aus
}

# Upgrade-Sicherung: bisherige Teile nach .install-alt\ VERSCHIEBEN (kein Kopieren, kein Loeschen).
# Labtest 0.2.46 (e3): windows\ gesperrt (cmd mit Arbeitsverzeichnis darin) - app\ war schon
# verschoben, windows\ nicht, die Rueckgabe ging verloren, die Ruecknahme meldete "unveraendert"
# und ein zweiter Lauf LOESCHTE die einzige Sicherung von app\. Deshalb:
#   1. Rest eines frueheren Laufs: erst zurueckholen, was am Platz fehlt; geloescht wird er nur,
#      wenn danach alle Teile am Platz sind - sonst Abbruch, die Sicherung bleibt.
#   2. Sperrtest VOR dem ersten Verschieben: jeder Teil wird probeweise umbenannt und zurueck.
#      Scheitert einer, wird nichts verschoben.
#   3. Die Ruecknahme liest, was WIRKLICH in .install-alt\ liegt (Get-RelaySicherungsTeile).
# Labtest 0.2.48 (e5): windows\ erst WAEHREND der Lebenszeichen-Wartezeit gesperrt - die Ruecknahme
# blieb unvollstaendig (richtig gemeldet), aber der naechste Lauf hielt die verworfene NEUE windows\
# am Platz fuer "alle Teile am Platz" und loeschte die Sicherung der alten. Deshalb ein Merker
# .install-alt\zurueckholen.json: welche Teile zurueck muessen (Fassung, Port). Ein Rest mit Merker
# wird zurueckgeholt - auch ueber ein belegtes Ziel - oder der Lauf bricht ab, ohne ihn anzufassen.
# Dazu traegt jeder kopierte Ordnerteil .relay-fassung (VERSION), damit sich pruefen laesst, wessen
# Teil am Platz liegt.
# Labtest 0.2.50 (Z3/Z1b): Move-Item auf einen ORDNER ist unter Windows PowerShell 5.1 NICHT
# atomar - bei einer offenen Datei darin (Share Read, kein Delete) wirft es, hat aber schon einen
# Teil der Dateien verschoben (der Sperrtest selbst zerlegte app\). Deshalb verschiebt install.ps1
# NUR ueber Move-RelayPfad: [IO.Directory]::Move bzw. [IO.File]::Move (MoveFileEx, ein Rename -
# scheitert ganz oder gar nicht) und nie auf ein vorhandenes Ziel (sonst landete der Ordner DARIN).
# Reste frueherer Laeufe (*.relay-sperrtest, *.relay-verworfen) fuehrt Repair-RelayReste dateiweise
# zurueck. Getestet in tests\test_install_abbruch.py und im CI-Job "Installer echt" (offene Dateien).
# Nur echte Inhalte: ein LEERER Ordnerrest (Loeschen scheiterte an einer Sperre darin, Labtest
# 0.2.49 Z2) ist nie eine Sicherung.
function Get-RelaySicherungsTeile([string]$Sicherung, [string[]]$Teile) {
    if (-not (Test-Path -LiteralPath $Sicherung)) { return }
    foreach ($teil in $Teile) {
        $p = Join-Path $Sicherung $teil
        if (Test-Path -LiteralPath $p -PathType Leaf) { $teil }
        elseif ((Test-Path -LiteralPath $p -PathType Container) -and @(Get-ChildItem -LiteralPath $p -Force -ErrorAction SilentlyContinue | Select-Object -First 1).Count -gt 0) { $teil }
    }
}

$RelayMerkerName = "zurueckholen.json"

# UTF-8 OHNE BOM - alles, was Python liest (Labtest 0.2.54: Set-Content -Encoding UTF8 schreibt
# unter PS 5.1 eine BOM; json.loads scheiterte, die offene Ruecknahme galt als "nichts offen").
function Write-RelayTextOhneBom([string]$Pfad, [string]$Text) {
    [System.IO.File]::WriteAllText($Pfad, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Write-RelayMerker([string]$Sicherung, [string[]]$Teile, [string]$Fassung, [string]$Port, [string]$Lauf = "") {
    $inhalt = [ordered]@{ teile = @($Teile); fassung = $Fassung; port = $Port; lauf = $Lauf; zeit = (Get-Date).ToString("s") }
    Write-RelayTextOhneBom (Join-Path $Sicherung $RelayMerkerName) (ConvertTo-Json -Compress -InputObject $inhalt)
}

function Read-RelayMerker([string]$Sicherung) {
    $m = Join-Path $Sicherung $RelayMerkerName
    if (-not (Test-Path -LiteralPath $m)) { return $null }
    try { return (Get-Content -LiteralPath $m -Raw | ConvertFrom-Json) } catch { return $null }
}

# .relay-fassung: Zeile 1 die Fassung, Zeile 2 "lauf=<ID>" - die Lauf-ID des install.ps1-Laufs, der
# den Teil kopiert hat (Labtest 0.2.51, Z4: dieselbe Fassung noch einmal installiert - an der Fassung
# allein ist Neu und Alt nicht zu unterscheiden).
function Get-RelayTeilFassung([string]$Pfad) {
    $f = Join-Path $Pfad ".relay-fassung"
    if (Test-Path -LiteralPath $f) { return "$(@(Get-Content -LiteralPath $f)[0])".Trim() }
    return ""
}

function Get-RelayTeilLauf([string]$Pfad) {
    $f = Join-Path $Pfad ".relay-fassung"
    if (-not (Test-Path -LiteralPath $f)) { return "" }
    foreach ($z in @(Get-Content -LiteralPath $f)) { if ("$z" -match '^lauf=(\w+)$') { return $Matches[1] } }
    return ""
}

function Set-RelayTeilMarke([string]$Ordner, [string]$Fassung, [string]$Lauf) {
    Set-Content -LiteralPath (Join-Path $Ordner ".relay-fassung") -Value @($Fassung, "lauf=$Lauf") -Encoding ASCII
}

# Der EINZIGE Weg, in install.ps1 einen Ordner oder Teil zu verschieben: atomar, nie auf ein
# vorhandenes Ziel.
function Move-RelayPfad([string]$Von, [string]$Nach) {
    if (Test-Path -LiteralPath $Nach) { throw "Ziel existiert schon, nicht verschoben: $Nach" }
    if (Test-Path -LiteralPath $Von -PathType Container) {
        [System.IO.Directory]::Move($Von, $Nach)
    } else {
        [System.IO.File]::Move($Von, $Nach)
    }
}

function Get-RelayFreierName([string]$Basis) {
    $n = $Basis; $i = 1
    while (Test-Path -LiteralPath $n) { $n = "$Basis-$i"; $i++ }
    $n
}

# Dateien aus $Von dorthin, wo sie in $Nach FEHLEN - jede Datei fuer sich (atomar). Was in $Nach
# schon liegt, bleibt; was sich nicht bewegen laesst, zaehlt als offen. Leere Ordner in $Von
# werden danach entfernt. $Ohne: ein Unterordner von $Von, der ausgelassen wird.
function Merge-RelayOrdner([string]$Von, [string]$Nach, [string]$Ohne = "") {
    $erg = @{ Bewegt = 0; Offen = 0; Doppelt = 0 }
    if (-not (Test-Path -LiteralPath $Von -PathType Container)) { return $erg }
    $wurzel = [IO.Path]::GetFullPath($Von).TrimEnd("\", "/")
    $ohneVoll = ""
    if ($Ohne) { $ohneVoll = [IO.Path]::GetFullPath($Ohne).TrimEnd("\", "/") }
    foreach ($f in @(Get-ChildItem -LiteralPath $Von -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        if ($ohneVoll -and $f.FullName.StartsWith($ohneVoll + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { continue }
        $rel = $f.FullName.Substring($wurzel.Length).TrimStart("\", "/")
        $ziel = Join-Path $Nach $rel
        if (Test-Path -LiteralPath $ziel) { $erg.Doppelt++; continue }
        try {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ziel) | Out-Null
            [System.IO.File]::Move($f.FullName, $ziel)
            $erg.Bewegt++
        } catch { $erg.Offen++ }
    }
    foreach ($d in @(Get-ChildItem -LiteralPath $Von -Recurse -Directory -Force -ErrorAction SilentlyContinue | Sort-Object { $_.FullName.Length } -Descending)) {
        if (@(Get-ChildItem -LiteralPath $d.FullName -Force -ErrorAction SilentlyContinue).Count -eq 0) { Remove-Item -LiteralPath $d.FullName -Force -ErrorAction SilentlyContinue }
    }
    if (@(Get-ChildItem -LiteralPath $Von -Force -ErrorAction SilentlyContinue).Count -eq 0) { Remove-Item -LiteralPath $Von -Force -ErrorAction SilentlyContinue }
    $erg
}

# Selbstheilung beim Start (Labtest 0.2.50 Z3): Reste des nicht-atomaren Verschiebens frueherer
# Fassungen zurueckfuehren. *.relay-sperrtest war derselbe Teil (umbenannt) - fehlende Dateien
# kommen zurueck, auch aus einer verschachtelten Ebene <teil>.relay-sperrtest\<teil>.
# *.relay-verworfen ist eine verworfene NEUE Fassung - zurueck nur, wenn der Teil ganz fehlt,
# sonst weg.
function Repair-RelayReste([string]$Ziel, [string[]]$Teile) {
    foreach ($teil in $Teile) {
        $p = Join-Path $Ziel $teil
        foreach ($r in @(Get-ChildItem -LiteralPath $Ziel -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "$teil.relay-sperrtest*" })) {
            if (-not (Test-Path -LiteralPath $p)) {
                Move-RelayPfad $r.FullName $p
                Write-Warn "Rest eines frueheren Laufs zurueckgefuehrt: $($r.Name) -> $teil"
                continue
            }
            if (-not $r.PSIsContainer) { Remove-Item -LiteralPath $r.FullName -Force -ErrorAction SilentlyContinue; continue }
            $nest = Join-Path $r.FullName $teil
            $e1 = Merge-RelayOrdner $nest $p
            $e2 = Merge-RelayOrdner $r.FullName $p $nest
            $n = $e1.Bewegt + $e2.Bewegt
            if (Test-Path -LiteralPath $r.FullName) {
                Write-Warn "Rest $($r.Name): $n Datei(en) nach $teil\ zurueckgefuehrt, Rest bleibt ($($e1.Offen + $e2.Offen) gesperrt, $($e1.Doppelt + $e2.Doppelt) doppelt)"
            } else {
                Write-Warn "Rest $($r.Name): $n Datei(en) nach $teil\ zurueckgefuehrt"
            }
        }
        foreach ($r in @(Get-ChildItem -LiteralPath $Ziel -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "$teil.relay-verworfen*" })) {
            if (-not (Test-Path -LiteralPath $p)) {
                Move-RelayPfad $r.FullName $p
                Write-Warn "Teil fehlte, verworfene Fassung zurueck an den Platz: $($r.Name) -> $teil"
                continue
            }
            Remove-Item -LiteralPath $r.FullName -Recurse -Force -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $r.FullName) { Write-Warn "Verworfene Fassung bleibt liegen (gesperrt): $($r.Name)" }
        }
    }
}

function Test-RelayVerschiebbar([string]$Pfad) {
    $probe = Get-RelayFreierName "$Pfad.relay-sperrtest"
    try {
        Move-RelayPfad $Pfad $probe
    } catch {
        return $false
    }
    Move-RelayPfad $probe $Pfad
    return $true
}

function Backup-RelayFassung([string]$Ziel, [string]$Sicherung, [string[]]$Teile, [string]$Fassung = "", [string]$Port = "", [string]$Lauf = "") {
    Repair-RelayReste $Ziel $Teile
    if (Test-Path -LiteralPath $Sicherung) {
        $rest = @(Get-RelaySicherungsTeile $Sicherung $Teile)
        $merker = Read-RelayMerker $Sicherung
        if ($merker) {
            # Alles laut Merker zurueck - ein belegtes Ziel ist die verworfene neue Fassung.
            $offen = @(Restore-RelayFassung -Ziel $Ziel -Sicherung $Sicherung -Teile @($merker.teile))
            if ($offen.Count -gt 0) {
                throw "Rest eines abgebrochenen Laufs (Fassung $($merker.fassung)) nicht zurueckholbar: $($offen -join ', ') - Sperre loesen, die Sicherung $Sicherung bleibt"
            }
            Write-Warn "Aus dem Rest eines frueheren Laufs zurueckgeholt (Fassung $($merker.fassung)): $(@($merker.teile | Where-Object { $rest -contains $_ }) -join ', ')"
        } else {
            # Ohne Merker (Lauf vor 0.2.49 oder Absturz vor dem Schreiben): nur fehlende Teile.
            foreach ($teil in $rest) {
                $p = Join-Path $Ziel $teil
                if (-not (Test-Path -LiteralPath $p)) {
                    Move-RelayPfad (Join-Path $Sicherung $teil) $p
                    Write-Warn "Aus dem Rest eines frueheren Laufs zurueckgeholt: $teil"
                }
            }
        }
        if (Test-Path -LiteralPath $Sicherung) {
            Remove-Item -LiteralPath $Sicherung -Recurse -Force -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $Sicherung) {
                throw "Rest eines frueheren Laufs nicht entfernbar (gesperrt?): $Sicherung - Sperre loesen, nichts verschoben"
            }
            Write-Warn "Rest eines frueheren Laufs entfernt (alle Teile am Platz): $Sicherung"
        }
    }
    $gesperrt = @()
    foreach ($teil in $Teile) {
        $p = Join-Path $Ziel $teil
        if ((Test-Path -LiteralPath $p) -and -not (Test-RelayVerschiebbar $p)) { $gesperrt += $teil }
    }
    if ($gesperrt.Count -gt 0) {
        throw "gesperrt (offene Datei oder Arbeitsverzeichnis eines Programms darin): $($gesperrt -join ', ') - nichts verschoben"
    }
    New-Item -ItemType Directory -Force -Path $Sicherung | Out-Null
    $vorhanden = @($Teile | Where-Object { Test-Path -LiteralPath (Join-Path $Ziel $_) })
    Write-RelayMerker $Sicherung $vorhanden $Fassung $Port $Lauf
    foreach ($teil in $vorhanden) {
        Move-RelayPfad (Join-Path $Ziel $teil) (Join-Path $Sicherung $teil)
        $teil
    }
}

# Gegenstueck: was in .install-alt\ liegt, zurueck an seinen Platz. Rueckgabe: Teile, die NICHT
# zurueckkamen; die Sicherung bleibt dann liegen (Merker auf diese Teile gekuerzt).
#   * Teil fehlt am Platz:                 die Sicherung wird verschoben (atomar).
#   * am Platz die NEUE Fassung (.relay-fassung <> Fassung im Merker): beiseite (eindeutiger Name,
#     nie geloescht), Sicherung hinein; scheitert das, kommt die neue zurueck (Labtest 0.2.49 Z1).
#   * am Platz ein Teil der ALTEN Fassung (gleiche Marke oder keine - neue tragen immer eine):
#     fehlende Dateien aus der Sicherung ergaenzen (Labtest 0.2.50 Z1b, Teilverschiebung).
#     NIE fuer den Lauf, der die Sicherung angelegt hat: Traegt der Teil am Platz dessen Lauf-ID
#     (Merker "lauf"), ist er dessen NEUER Stand - immer tauschen (Labtest 0.2.51 Z4: gleiche
#     Fassung neu installiert, "ergaenzt" statt zurueckgerollt).
function Restore-RelayFassung([string]$Ziel, [string]$Sicherung, [string[]]$Teile) {
    $offen = @()
    $merker = Read-RelayMerker $Sicherung
    $altFassung = ""
    if ($merker) { $altFassung = "$($merker.fassung)" }
    foreach ($teil in @(Get-RelaySicherungsTeile $Sicherung $Teile)) {
        $p = Join-Path $Ziel $teil
        $s = Join-Path $Sicherung $teil
        if (-not (Test-Path -LiteralPath $p)) {
            try { Move-RelayPfad $s $p } catch { $offen += $teil }
            continue
        }
        $istOrdner = Test-Path -LiteralPath $p -PathType Container
        $platzFassung = ""
        if ($istOrdner) { $platzFassung = Get-RelayTeilFassung $p }
        $merkerLauf = ""
        if ($merker -and ($merker.PSObject.Properties.Name -contains "lauf")) { $merkerLauf = "$($merker.lauf)" }
        $istNeu = (-not $istOrdner) -or ($platzFassung -and $platzFassung -ne $altFassung) -or
                  ($merkerLauf -and $istOrdner -and (Get-RelayTeilLauf $p) -eq $merkerLauf)
        if (-not $istNeu) {
            $e = Merge-RelayOrdner $s $p
            if ($e.Bewegt -gt 0) { Write-Warn "${teil}\: $($e.Bewegt) fehlende Datei(en) aus der Sicherung ergaenzt" }
            # Doppelte Dateien bleiben wie am Platz (gleiche Fassung); offen nur, was sich nicht bewegen liess.
            if ($e.Offen -gt 0) { $offen += $teil }
            elseif (Test-Path -LiteralPath $s) { Remove-Item -LiteralPath $s -Recurse -Force -ErrorAction SilentlyContinue }
            continue
        }
        $weg = Get-RelayFreierName "$p.relay-verworfen"
        try { Move-RelayPfad $p $weg } catch { $offen += $teil; continue }
        try {
            Move-RelayPfad $s $p
        } catch {
            try { Move-RelayPfad $weg $p } catch { Write-Warn "$teil fehlt am Platz und liegt unter $weg - der naechste Lauf holt es zurueck" }
            $offen += $teil
            continue
        }
        Remove-Item -LiteralPath $weg -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $weg) { Write-Warn "Verworfene neue Fassung bleibt liegen (gesperrt): $weg" }
    }
    if ($offen.Count -eq 0) {
        Remove-Item -LiteralPath $Sicherung -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        $f = ""; $po = ""
        $la = ""
        if ($merker) { $f = "$($merker.fassung)"; $po = "$($merker.port)"; if ($merker.PSObject.Properties.Name -contains "lauf") { $la = "$($merker.lauf)" } }
        Write-RelayMerker $Sicherung $offen $f $po $la
    }
    $offen
}

# Aus dem catch des Hauptablaufs: vorige Fassung zurueck, Abhaengigkeiten zurueck, Dienst wieder
# starten, wenn er vorher lief. Liest die Ablaufvariablen ($gesichert, $pipGelaufen ...).
function Restore-RelayNachAbbruch {
    try { Stop-Service -Name "ExoSmtpRelay" -Force -ErrorAction SilentlyContinue } catch { }
    if ((Test-Path variable:script:RelayOffenInhalt) -and $script:RelayOffenInhalt) {
        try {
            Write-RelayTextOhneBom (Join-Path $dataDir "update\ruecknahme_offen.json") $script:RelayOffenInhalt
            Write-Warn "Offene Ruecknahme des Updaters wiederhergestellt"
        } catch { }
    }
    # Zurueckgeholt wird nur, was DIESER Lauf verschoben hat oder ein Merker nennt - nie ein Rest
    # ohne Merker (Labtest 0.2.49 Z2: leerer Rest galt als Sicherung, das GUTE windows\ flog raus).
    $inSicherung = @()
    if (@($gesichert).Count -gt 0 -or (Read-RelayMerker $sicherung)) { $inSicherung = @(Get-RelaySicherungsTeile $sicherung $RelayTeile) }
    if ($inSicherung.Count -gt 0) {
        $offen = @(Restore-RelayFassung -Ziel $InstallDir -Sicherung $sicherung -Teile $RelayTeile)
        if ($offen.Count -eq 0) {
            Write-Warn "Vorige Fassung $altVersion zurueckgeholt ($($inSicherung -join ', '))"
        } else {
            Write-Warn "Ruecknahme UNVOLLSTAENDIG: $($offen -join ', ') liegt noch in $sicherung - Sperre loesen und install.ps1 erneut ausfuehren (die Sicherung bleibt erhalten)"
        }
        foreach ($teil in @("app", "windows")) {
            $tf = Get-RelayTeilFassung (Join-Path $InstallDir $teil)
            if ($tf -and $altVersion -and $tf -ne $altVersion) { Write-Warn "$teil\ am Platz stammt aus Fassung $tf (nicht $altVersion)" }
        }
        # Abhaengigkeiten, Port in Dienst-Umgebung UND Firewall folgen EINER Regel: dem Code, der
        # laeuft - also app\. Ist app\ zurueck, alles zurueck; sonst nichts (Labtest 0.2.48 e5a:
        # Firewall alt, WEBUI_PORT neu).
        $appZurueck = ($inSicherung -contains "app") -and -not ($offen -contains "app")
        $lock = Join-Path $InstallDir "app\requirements.lock"
        if ($appZurueck -and $pipGelaufen -and (Test-Path $venvPython) -and (Test-Path $lock)) {
            try {
                $pipArgs = @(Get-RelayPipArgumente -InstallDir $InstallDir -Lock $lock)
                Invoke-RelayNativ "pip (Ruecknahme)" { & $venvPython @pipArgs } | Out-Null
                Write-Warn "Abhaengigkeiten auf Fassung $altVersion zurueckgesetzt"
            } catch {
                Write-Warn "Abhaengigkeiten nicht zurueckgesetzt: $($_.Exception.Message)"
            }
        }
        if ($appZurueck -and $altPort) {
            if ($umgebungGesetzt) {
                try {
                    $sp = Join-Path $venv "Lib\site-packages"
                    $du = Join-Path $InstallDir "windows\dienst_umgebung.py"
                    if (-not (Test-Path $du)) { $du = Join-Path $sicherung "windows\dienst_umgebung.py" }
                    Invoke-RelayNativ "Dienst-Umgebung (Ruecknahme)" { & $venvPython $du --setzen "PYTHONPATH=$sp;$sp\win32;$sp\win32\lib" --port "$altPort" } | Out-Null
                    Write-Warn "Web-Port der Dienst-Umgebung wieder $altPort"
                } catch { Write-Warn "Dienst-Umgebung nicht zurueckgesetzt: $($_.Exception.Message)" }
            }
            if ($firewallGesetzt) {
                try { Sync-RelayFirewall -WebPort $altPort; Write-Warn "Firewall wieder auf Port $altPort" }
                catch { Write-Warn "Firewall nicht zurueckgesetzt: $($_.Exception.Message)" }
            }
        } elseif (-not $appZurueck -and ($umgebungGesetzt -or $firewallGesetzt)) {
            Write-Warn "Web-Port bleibt wie eingestellt (Dienst-Umgebung und Firewall), weil app\ nicht zurueckkam"
        }
    } elseif ($istUpgrade) {
        Write-Warn "Abbruch vor dem Austausch - nichts verschoben, die installierte Fassung ist unveraendert."
    } else {
        Write-Warn "Erstinstallation unvollstaendig - Ursache beheben und install.ps1 erneut ausfuehren."
    }
    if ($dienstLief) {
        try {
            Start-RelayDienst -DataDir $dataDir
            $v = ""
            if (Test-Path (Join-Path $InstallDir "VERSION")) { $v = (Get-Content (Join-Path $InstallDir "VERSION") -Raw).Trim() }
            Write-Warn "Dienst laeuft wieder (Fassung $v)"
        } catch {
            Write-Warn "Dienst startet nicht: $($_.Exception.Message) - Protokoll: $dataDir\logs\app.log"
        }
    }
}

# Dienst starten - bis zu $Versuche Mal (Release v0.2.52-beta: einmaliger "Failed to start service"
# auf dem CI-Runner, in vier Wiederholungen desselben Pakets nicht wieder aufgetreten). Jeder
# Fehlschlag wird mit dem SCM-Ereignis und dem Ende von service.log gemeldet; erst wenn alle
# Versuche scheitern, bricht der Lauf ab (Ruecknahme). Ob die Fassung TRAEGT, entscheidet danach
# ohnehin das Lebenszeichen (/health).
function Get-RelayLogLaenge([string]$DataDir) {
    $log = Join-Path $DataDir "logs\service.log"
    if (Test-Path -LiteralPath $log) { return (Get-Item -LiteralPath $log).Length }
    return 0
}

# service.log nur AB $AbByte (Startzeitpunkt) und als UTF-8 (so schreibt der Dienst-Wrapper sie,
# PYTHONIOENCODING=utf-8) - sonst standen alte Zeilen vom Vortag in falscher Kodierung da.
function Get-RelayStartGrund([string]$DataDir, [long]$AbByte = 0) {
    $teile = @()
    try {
        $e = @(Get-WinEvent -FilterHashtable @{ LogName = "System"; ProviderName = "Service Control Manager"; StartTime = (Get-Date).AddMinutes(-5) } -MaxEvents 20 -ErrorAction Stop |
            Where-Object { $_.Message -match "EXO SMTP Relay|ExoSmtpRelay" -and $_.Id -in @(7000, 7009, 7011, 7023, 7024, 7031, 7034) } | Select-Object -First 1)
        if ($e.Count) { $teile += "SCM $($e[0].Id): $(($e[0].Message -replace '\s+', ' ').Trim())" }
    } catch { }
    $log = Join-Path $DataDir "logs\service.log"
    if (Test-Path -LiteralPath $log) {
        try {
            $fs = [System.IO.File]::Open($log, "Open", "Read", "ReadWrite, Delete")
            try {
                if ($AbByte -gt $fs.Length) { $AbByte = 0 }
                [void]$fs.Seek($AbByte, "Begin")
                $puffer = New-Object byte[] ([int]($fs.Length - $AbByte))
                $gelesen = $fs.Read($puffer, 0, $puffer.Length)
                $text = [System.Text.Encoding]::UTF8.GetString($puffer, 0, $gelesen)
            } finally { $fs.Close() }
            $ende = @($text -split "\r?\n" | ForEach-Object { "$_".Trim() } | Where-Object { $_ } | Select-Object -Last 4)
            if ($ende.Count) { $teile += "service.log: $($ende -join ' | ')" }
        } catch { }
    }
    if ($teile.Count -eq 0) { return "keine weiteren Angaben (SCM-Ereignis/service.log leer)" }
    $teile -join " ; "
}

function Start-RelayDienst([string]$DataDir, [int]$Versuche = 3, [int]$PauseS = 10) {
    for ($i = 1; $i -le $Versuche; $i++) {
        $fehler = ""
        $ab = Get-RelayLogLaenge $DataDir
        try {
            Start-Service -Name "ExoSmtpRelay" -ErrorAction Stop
            Start-Sleep -Seconds 3
            $st = "$((Get-Service -Name "ExoSmtpRelay").Status)"
            if ($st -eq "Running") { return }
            $fehler = "Status nach dem Start: $st"
        } catch { $fehler = "$($_.Exception.Message)" }
        $grund = Get-RelayStartGrund $DataDir $ab
        if ($i -lt $Versuche) {
            Write-Warn "Dienststart, Versuch $i von ${Versuche}: $fehler - $grund; neuer Versuch in $PauseS s"
            try { Stop-Service -Name "ExoSmtpRelay" -Force -ErrorAction SilentlyContinue } catch { }
            Start-Sleep -Seconds $PauseS
        } else {
            throw "Dienst startet nicht ($Versuche Versuche): $fehler - $grund"
        }
    }
}

# Lebenszeichen der NEUEN Fassung: /health auf dem wirksamen Port muss die Soll-Version melden
# (Labtest 0.2.46, e4: main.py warf beim Import, der Dienst war "Running", nichts lauschte -
# trotzdem "Aktualisiert"). Abfrage mit dem Python der venv (kein curl auf Server 2016 noetig);
# das selbstsignierte Zertifikat wird dabei bewusst nicht geprueft - nur 127.0.0.1.
function Wait-RelayGesund([string]$Python, [int]$Port, [string]$Version, [int]$Sekunden = 60) {
    $code = "import json,ssl,sys,urllib.request`nk=ssl._create_unverified_context()`nfor u in ('https','http'):`n    try:`n        print(json.load(urllib.request.urlopen(u+'://127.0.0.1:$Port/health',timeout=3,context=k)).get('version',''));sys.exit(0)`n    except Exception: pass`nprint('')"
    $ende = (Get-Date).AddSeconds($Sekunden)
    $zuletzt = ""
    while ((Get-Date) -lt $ende) {
        try { $zuletzt = "$(@(Invoke-RelayNativ "health" { & $Python -c $code })[-1])".Trim() } catch { $zuletzt = "" }
        if ($zuletzt -eq $Version) { return }
        Start-Sleep -Seconds 2
    }
    throw "/health auf Port $Port meldet nach $Sekunden s nicht $Version (zuletzt: '$zuletzt') - Protokoll: $dataDir\logs\service.log"
}

# Python-Fassungen, fuer die app\requirements.lock Wheels hat (pywin32, pydantic_core, cffi ...).
# Eine neuere Fassung baut eine venv, in die pip nichts installieren kann.
$RelayPythonMin = "3.11"
$RelayPythonMax = "3.13"

# "3.12" (bzw. mit -Pfad sys.executable) - oder "" bei jedem Fehler. EAP lokal Continue: eine
# stderr-Zeile des Kandidaten ist unter PS 5.1 sonst ein beendender Fehler.
function Get-RelayPythonFassung([string]$Exe, [string[]]$Zusatz, [switch]$Pfad) {
    $code = if ($Pfad) { "import sys; print(sys.executable)" } else { "import sys; print('%d.%d' % sys.version_info[:2])" }
    $vorher = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $aus = @(& $Exe @($Zusatz + @("-c", $code)) 2>$null)
        if ($LASTEXITCODE -ne 0 -or $aus.Count -eq 0) { return "" }
        return "$($aus[-1])".Trim()
    } catch { return "" } finally { $ErrorActionPreference = $vorher }
}

function Test-RelayPythonPasst([string]$Fassung) {
    if ($Fassung -notmatch '^\d+\.\d+$') { return $false }
    return ([version]$Fassung -ge [version]$RelayPythonMin) -and ([version]$Fassung -le [version]$RelayPythonMax)
}

# pythonservice.exe liegt im venv-Stamm (pywin32 kopiert es dorthin) und laedt python3XX.dll.
# Die findet es nur ueber den Maschinen-PATH (Python-Installer "Add python.exe to PATH") - sonst
# haengt der Dienst beim Start bis zum Zeitlimit (CI 0.2.46: SCM 7009, Python ohne PATH-Eintrag).
# Dann legt der Installer die DLLs neben pythonservice.exe und merkt sich, welche; liegt die
# Basis (wieder) im PATH, entfernt er genau diese Kopien. Getestet in tests\test_install_abbruch.py.
function Sync-RelayDienstDlls([string]$Venv, [string]$Basis, [string]$MaschinenPfad) {
    $merker = Join-Path $Venv "relay-dienst-dlls.txt"
    $bisher = @()
    if (Test-Path -LiteralPath $merker) { $bisher = @(Get-Content -LiteralPath $merker | Where-Object { $_ }) }
    $norm = $Basis.TrimEnd("\", "/")
    $imPfad = @($MaschinenPfad -split ";" | ForEach-Object { $_.Trim().TrimEnd("\", "/") } | Where-Object { $_ -ieq $norm }).Count -gt 0
    if ($imPfad) {
        foreach ($n in $bisher) { Remove-Item -LiteralPath (Join-Path $Venv $n) -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $merker -Force -ErrorAction SilentlyContinue
        Write-Ok "Python-DLL ueber den Maschinen-PATH ($Basis)"
        return
    }
    $dlls = @(Get-ChildItem -LiteralPath $Basis -Filter "*.dll" | Where-Object { $_.Name -match '^(python3\d*|vcruntime140(_1)?)\.dll$' })
    if ($dlls.Count -eq 0) { throw "Keine python3*.dll unter $Basis - Dienst koennte nicht starten." }
    foreach ($d in $dlls) { Copy-Item -LiteralPath $d.FullName -Destination (Join-Path $Venv $d.Name) -Force }
    foreach ($n in $bisher) {
        if (@($dlls | Where-Object { $_.Name -eq $n }).Count -eq 0) { Remove-Item -LiteralPath (Join-Path $Venv $n) -Force -ErrorAction SilentlyContinue }
    }
    Set-Content -LiteralPath $merker -Value @($dlls | ForEach-Object { $_.Name }) -Encoding ASCII
    Write-Ok "Python-DLLs neben pythonservice.exe gelegt ($Basis steht nicht im Maschinen-PATH): $(($dlls | ForEach-Object { $_.Name }) -join ', ')"
}

# -- Feste Quellen der Lieferkette ------------------------------------------------
# Nichts wird ungeprueft ausgefuehrt: Jede Datei von aussen hat hier Adresse, SHA256 und den
# erwarteten Authenticode-Signierer. Dieselben Eintraege liest tools\offline_paket.py fuer das
# Offline-Paket. AKTUALISIEREN: docs/release-test.md, Abschnitt "Lieferkette".
#   Python: SHA256 selbst berechnet, MD5 gegen die python.org-API und die PGP-Signatur (.asc,
#   Schluessel 7ED10B6531D7C8E1BC296021FC624643487034E5 laut python.org/downloads/metadata/pgp)
#   gegengeprueft am 24.11.2026. Nur x64: cryptography hat kein Windows-ARM64-Wheel.
$RelayPythonInstaller = @{
    Version   = "3.12.10"
    Datei     = "python-3.12.10-amd64.exe"
    Url       = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
    Sha256    = "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"
    Signierer = "Python Software Foundation"
}
#   ExchangeOnlineManagement: unveraendertes nupkg der PowerShell Gallery (Fassung wie Gateway
#   und Dockerfile), Lizenztext im Paket (license.txt, MIT-Text). Skriptdateien darin tragen eine
#   Authenticode-Signatur von Microsoft.
$RelayExoModul = @{
    Name      = "ExchangeOnlineManagement"
    Version   = "3.10.1"
    Datei     = "exchangeonlinemanagement.3.10.1.nupkg"
    Url       = "https://www.powershellgallery.com/api/v2/package/ExchangeOnlineManagement/3.10.1"
    Sha256    = "545fb0fdf65b96abed37f4f5b5d7db661276ddc7de48a742bb6e7199ab9414a3"
    Signierer = "Microsoft Corporation"
}

# Authenticode: Status Valid UND der erwartete Signierer (CN oder O im Zertifikat).
function Test-RelaySignatur([string]$Pfad, [string]$Signierer) {
    $sig = Get-AuthenticodeSignature -FilePath $Pfad
    $name = Split-Path -Leaf $Pfad
    if ("$($sig.Status)" -ne "Valid") {
        throw "${name}: Authenticode-Signatur nicht gueltig ($($sig.Status): $($sig.StatusMessage)) - Datei NICHT verwendet."
    }
    $subjekt = "$($sig.SignerCertificate.Subject)"
    if ($subjekt -notmatch "(^|,\s*)(CN|O)=$([regex]::Escape($Signierer))(,|$)") {
        throw "${name}: signiert von '$subjekt', erwartet '$Signierer' - Datei NICHT verwendet."
    }
}

# Feste Pruefsumme (und Signatur) einer Datei aus $RelayPythonInstaller / $RelayExoModul.
function Test-RelayDatei([string]$Pfad, [hashtable]$Quelle, [switch]$OhneSignatur) {
    if (-not (Test-Path -LiteralPath $Pfad)) { throw "$($Quelle.Datei) fehlt: $Pfad" }
    $ist = (Get-FileHash -LiteralPath $Pfad -Algorithm SHA256).Hash
    if ($ist -ne $Quelle.Sha256) {
        throw "$($Quelle.Datei): Pruefsumme stimmt nicht (erwartet $($Quelle.Sha256), erhalten $ist) - Datei NICHT verwendet."
    }
    if (-not $OhneSignatur) { Test-RelaySignatur $Pfad $Quelle.Signierer }
}

# Der EINZIGE Download-Weg. In der Offline-Installation verweigert er.
function Get-RelayDownload([string]$Url, [string]$Ziel) {
    if ($script:RelayOffline) { throw "Offline-Installation: kein Download ($Url)" }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -Uri $Url -OutFile $Ziel -UseBasicParsing -ErrorAction Stop
}

function Install-RelayPython([string]$Installer) {
    Test-RelayDatei $Installer $RelayPythonInstaller
    Write-Ok "$($RelayPythonInstaller.Datei): Pruefsumme und Signatur ($($RelayPythonInstaller.Signierer)) gueltig"
    $p = Start-Process -FilePath $Installer -Wait -PassThru -ArgumentList @("/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_test=0")
    if ($p.ExitCode -notin @(0, 3010)) { throw "Python-Installer: Exit $($p.ExitCode)" }
}

# nupkg pruefen, entpacken (ohne NuGet-Verwaltungsdateien), jede Skriptdatei auf die
# Microsoft-Signatur pruefen, erst dann nach Program Files\WindowsPowerShell\Modules.
function Install-RelayExoModul([string]$Nupkg) {
    $em = $RelayExoModul
    Test-RelayDatei $Nupkg $em -OhneSignatur          # nupkg selbst: Pruefsumme; Inhalt: Authenticode
    $ziel = Join-Path $env:ProgramFiles "WindowsPowerShell\Modules\$($em.Name)\$($em.Version)"
    $tmp = "$ziel.neu"
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $wurzel = [IO.Path]::GetFullPath($tmp).TrimEnd("\") + "\"
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Nupkg)
    try {
        foreach ($e in $zip.Entries) {
            $name = [Uri]::UnescapeDataString($e.FullName)
            if ($name.EndsWith("/") -or $name -match '^(_rels/|package/)' -or $name -match '^\[Content_Types\]\.xml$' -or $name -match '\.nuspec$') { continue }
            $pfad = [IO.Path]::GetFullPath((Join-Path $tmp $name))
            if (-not $pfad.StartsWith($wurzel, [StringComparison]::OrdinalIgnoreCase)) { throw "Unzulaessiger Pfad im Modulpaket: $name" }
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $pfad) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $pfad, $true)
        }
    } finally { $zip.Dispose() }
    $skripte = @(Get-ChildItem -LiteralPath $tmp -Recurse -File | Where-Object { $_.Extension -in @(".ps1", ".psm1", ".psd1") })
    if ($skripte.Count -eq 0) { throw "$($em.Datei): keine Skriptdateien im Modul" }
    foreach ($s in $skripte) { Test-RelaySignatur $s.FullName $em.Signierer }
    # Nie "erst loeschen, dann ersetzen" (ein gesperrtes Modul waere halb geloescht): beiseite, rein, weg.
    $weg = ""
    if (Test-Path -LiteralPath $ziel) { $weg = Get-RelayFreierName "$ziel.alt"; Move-RelayPfad $ziel $weg }
    try { Move-RelayPfad $tmp $ziel } catch { if ($weg) { Move-RelayPfad $weg $ziel }; throw }
    if ($weg) { Remove-Item -LiteralPath $weg -Recurse -Force -ErrorAction SilentlyContinue }
    return $ziel
}

# pip-Argumente - DIESELBEN wie app\update_tausch.pip_argumente (Updater): nur Wheels, nur mit
# den Pruefsummen der Lock-Datei; liegt offline\wheels in der Installation, ohne Index daraus.
# Gleichheit prueft tests\test_abhaengigkeiten.py.
function Get-RelayPipArgumente([string]$InstallDir, [string]$Lock) {
    $a = @("-m", "pip", "install", "--quiet", "--disable-pip-version-check")
    # Lock ohne Pruefsummen: nur Fassungen vor 0.2.47 (Ruecknahme dorthin, Labtest 0.2.48 e6a).
    $mitHashes = $true
    if (Test-Path -LiteralPath $Lock) { $mitHashes = [bool](Select-String -LiteralPath $Lock -Pattern "--hash=sha256:" -SimpleMatch -Quiet) }
    if ($mitHashes) { $a += @("--require-hashes", "--only-binary", ":all:") }
    $o = Join-Path $InstallDir "offline"
    if ((Test-Path -LiteralPath (Join-Path $o "inhalt.json")) -and (Test-Path -LiteralPath (Join-Path $o "wheels"))) {
        $a += @("--no-index", "--find-links", (Join-Path $o "wheels"))
    }
    $a + @("-r", $Lock)
}

# -- Administrator? ---------------------------------------------------------------
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Bitte als Administrator ausfuehren (Dienst und Firewall-Regeln brauchen das)."
}

# -- Quelle: das Verzeichnis ueber diesem Skript --------------------------------
$quelle = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path (Join-Path $quelle "app\main.py"))) {
    throw "app\main.py nicht gefunden unter $quelle - das Skript gehoert nach windows\ im Anwendungsbaum."
}

# -- Offline-Paket? (exo-smtp-relay-<version>-windows-offline.zip) ------------------
# offline\inhalt.json + offline\wheels\ + offline\python\ + offline\module\: Python, alle
# Wheels und ExchangeOnlineManagement liegen bei - es wird NICHTS heruntergeladen
# (Get-RelayDownload verweigert dann), pip laeuft mit --no-index aus offline\wheels.
$script:RelayOffline = (Test-Path (Join-Path $quelle "offline\inhalt.json")) -and (Test-Path (Join-Path $quelle "offline\wheels"))
if ($RelayOffline) {
    Write-Step "Offline-Installation: Python, Wheels und ExchangeOnlineManagement aus dem Paket - kein Download"
}

# -- Python finden oder installieren ------------------------------------------------
Write-Step "Suche Python $RelayPythonMin bis $RelayPythonMax"
if ($PythonExe -ne "") {
    $ver = Get-RelayPythonFassung $PythonExe @()
    if (-not (Test-RelayPythonPasst $ver)) {
        throw "-PythonExe $PythonExe ist Python '$ver' - unterstuetzt sind $RelayPythonMin bis $RelayPythonMax (dafuer hat requirements.lock Wheels)."
    }
} else {
    # Ausdrueckliche Unterfassungen zuerst: "py -3" nimmt die HOECHSTE installierte Fassung
    # (CI 0.2.46: 3.15 - pywin32 311 hat dafuer kein Wheel, pip scheitert).
    $kandidaten = @()
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($u in @("-3.13", "-3.12", "-3.11", "-3")) { $kandidaten += @{ Exe = $py.Source; Args = @($u) } }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { $kandidaten += @{ Exe = $python.Source; Args = @() } }
    foreach ($k in $kandidaten) {
        $ver = Get-RelayPythonFassung $k.Exe $k.Args
        if (Test-RelayPythonPasst $ver) {
            $PythonExe = (Get-RelayPythonFassung $k.Exe $k.Args -Pfad)
            if ($PythonExe) { break }
        }
    }
}

if ($PythonExe -eq "" -or -not (Test-Path $PythonExe)) {
    Write-Warn "Python $RelayPythonMin bis $RelayPythonMax nicht gefunden."
    $pq = $RelayPythonInstaller
    if ($RelayOffline) {
        $installer = Join-Path $quelle "offline\python\$($pq.Datei)"
        $antwort = "j"
    } else {
        $installer = Join-Path $env:TEMP $pq.Datei
        $antwort = if ($Unattended) { "j" } else { Read-Host "Python $($pq.Version) von python.org laden, pruefen und installieren? (j/n)" }
    }
    if ($antwort -match "^[jJyY]") {
        if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") {
            Write-Warn "Windows auf ARM64: es kommt Python fuer x64 (Emulation) - cryptography hat kein ARM64-Wheel fuer Windows."
        }
        try {
            if (-not $RelayOffline) {
                Write-Step "Lade Python $($pq.Version) herunter"
                Write-Host "    Quelle: $($pq.Url)" -ForegroundColor Gray
                Get-RelayDownload $pq.Url $installer
            }
            Write-Step "Pruefe und installiere Python $($pq.Version)"
            Install-RelayPython $installer
            if (-not $RelayOffline) { Remove-Item $installer -ErrorAction SilentlyContinue }
            $py = Get-Command py -ErrorAction SilentlyContinue
            if ($py) { $PythonExe = Get-RelayPythonFassung $py.Source @("-$(($pq.Version -split '\.')[0..1] -join '.')") -Pfad }
            if ($PythonExe -and (Test-Path $PythonExe)) {
                Write-Ok "Python installiert: $PythonExe"
            } else {
                throw "Python-Installation fehlgeschlagen oder nicht auffindbar"
            }
        } catch {
            Write-Host ""
            Write-Host "Python nicht installiert: $($_.Exception.Message)" -ForegroundColor Yellow
            Write-Host "Von Hand: https://www.python.org/downloads/windows/" -ForegroundColor Yellow
            Write-Host "  * Python 3.11, 3.12 oder 3.13 waehlen (neuere haben noch keine passenden Pakete)" -ForegroundColor Yellow
            Write-Host "  * Haken bei 'Add python.exe to PATH' setzen" -ForegroundColor Yellow
            Write-Host "  * Installation abschliessen und dann dieses Skript erneut ausfuehren" -ForegroundColor Yellow
            throw "Python erforderlich: $($_.Exception.Message)"
        }
    } else {
        throw "Python 3.11 bis 3.13 erforderlich. Bitte von https://www.python.org/downloads/windows/ installieren (Haken 'Add python.exe to PATH')."
    }
}
Write-Ok "Python: $PythonExe"

# -- Dienst anhalten, falls vorhanden -------------------------------------------
$dienst = Get-Service -Name "ExoSmtpRelay" -ErrorAction SilentlyContinue
$istUpgrade = [bool]$dienst
$dienstLief = [bool]($dienst -and $dienst.Status -eq "Running")
if ($dienstLief) {
    Write-Step "Halte laufenden Dienst an"
    Stop-Service -Name "ExoSmtpRelay" -Force
    Write-Ok "angehalten"
}

# Ab hier ist der Dienst gestoppt. Jeder Fehler bis zum Start landet im catch unten: die
# vorige Fassung (app\, windows\, VERSION ...) kommt aus .install-alt\ zurueck, die
# Abhaengigkeiten werden auf deren Lock-Datei zurueckgesetzt und der Dienst startet wieder
# (Labtest 0.2.45: Abbruch nach dem Stopp liess ein gestopptes Relay mit neuer VERSION zurueck).
# Getestet in tests\test_install_abbruch.py und im CI-Job "Installer echt".
$venv = Join-Path $InstallDir "venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$dataDir = Join-Path $InstallDir "data"
$sicherung = Join-Path $InstallDir ".install-alt"
$altVersion = ""
if (Test-Path (Join-Path $InstallDir "VERSION")) { $altVersion = (Get-Content (Join-Path $InstallDir "VERSION") -Raw).Trim() }
$gesichert = @()
$script:RelayOffenInhalt = ""
# Lauf-ID: kennzeichnet, was DIESER Lauf kopiert (.relay-fassung) und gesichert hat (Merker).
$RelayLauf = [guid]::NewGuid().ToString("N")
$pipGelaufen = $false
$umgebungGesetzt = $false
$firewallGesetzt = $false
# Bisheriger Web-Port (nur lesen) - fuer die Ruecknahme von Dienst-Umgebung und Firewall.
$altPort = ""
try {
    $altUmgebung = @((Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\ExoSmtpRelay" -ErrorAction Stop).Environment)
    foreach ($e in $altUmgebung) { if ("$e" -match '^WEBUI_PORT=(\d+)$') { $altPort = $Matches[1] } }
} catch { }
$schritt = "Kopieren"
try {
    # -- Dateien kopieren -----------------------------------------------------------
    Write-Step "Kopiere Anwendung nach $InstallDir"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    $gesichert = @(Backup-RelayFassung -Ziel $InstallDir -Sicherung $sicherung -Teile $RelayTeile -Fassung $altVersion -Port $altPort -Lauf $RelayLauf)
    # Ordnerteile: erst Ordner und Fassungsmarke, DANN der Inhalt - ein abgebrochenes Kopieren
    # hinterlaesst nie einen neuen Teil ohne Marke (Ruecknahme unterscheidet neu/alt danach).
    $neuFassung = (Get-Content (Join-Path $quelle "VERSION") -Raw).Trim()
    foreach ($teil in $RelayTeile) {
        $q = Join-Path $quelle $teil
        if ($teil -eq "offline" -or -not (Test-Path $q)) { continue }
        $d = Join-Path $InstallDir $teil
        if (Test-Path $q -PathType Container) {
            New-Item -ItemType Directory -Force -Path $d | Out-Null
            Set-RelayTeilMarke $d $neuFassung $RelayLauf
            Copy-Item -Recurse -Force -Path (Join-Path $q "*") -Destination $d
        } else {
            Copy-Item -Force -LiteralPath $q -Destination $d
        }
    }
    # offline\: nur Wheels und inhalt.json (der Updater installiert daraus). Installer und
    # Modulpaket bleiben im Paket. Eine Online-Installation hat keins - war vorher eins da,
    # liegt es jetzt in .install-alt\ und verschwindet mit ihr.
    if ($RelayOffline) {
        $oz = Join-Path $InstallDir "offline"
        New-Item -ItemType Directory -Force -Path $oz | Out-Null
        Set-RelayTeilMarke $oz $neuFassung $RelayLauf
        Copy-Item -Force (Join-Path $quelle "offline\inhalt.json") (Join-Path $oz "inhalt.json")
        Copy-Item -Recurse -Force (Join-Path $quelle "offline\wheels") (Join-Path $oz "wheels")
    } elseif ($gesichert -contains "offline") {
        Write-Ok "Online-Installation: offline\ entfernt - Aktualisierungen laden kuenftig das normale Paket"
    }
    # __pycache__ aus der Quelle nicht mitschleppen
    Get-ChildItem -Path (Join-Path $InstallDir "app") -Recurse -Directory -Filter "__pycache__" |
        ForEach-Object { Remove-Item -Recurse -Force $_.FullName }
    Write-Ok "kopiert"

    # -- venv und Abhaengigkeiten -------------------------------------------------------
    $schritt = "venv"
    if (-not (Test-Path $venvPython)) {
        Write-Step "Erstelle virtuelle Umgebung"
        Invoke-RelayNativ "venv anlegen" { & $PythonExe -m venv $venv } | Out-Null
        Write-Ok "venv: $venv"
    }
    $schritt = "Abhaengigkeiten"
    Write-Step "Installiere Abhaengigkeiten (requirements.lock)"
    # pip aus der venv (kommt mit dem geprueften Python) - KEINE Selbstaktualisierung aus dem
    # Netz: die waere die einzige ungepruefte Datei. Nur Wheels, nur mit Pruefsummen.
    $pipGelaufen = $true
    $lock = Join-Path $InstallDir "app\requirements.lock"
    $pipArgs = @(Get-RelayPipArgumente -InstallDir $InstallDir -Lock $lock)
    if ($RelayOffline) { Write-Ok "Offline: pip --no-index aus offline\wheels" }
    Invoke-RelayNativ "pip install (requirements.lock)" { & $venvPython @pipArgs } | Out-Null
    # pywin32: Nachinstallation registriert die Dienst-DLLs
    $post = Join-Path $venv "Scripts\pywin32_postinstall.py"
    if (Test-Path $post) {
        try { Invoke-RelayNativ "pywin32_postinstall" { & $venvPython $post -install -silent } | Out-Null }
        catch { Write-Warn "$($_.Exception.Message)" }
    }
    Write-Ok "Abhaengigkeiten installiert"

    # -- Datenverzeichnis absichern: nur SYSTEM und Administratoren (nie beendend) --
    $schritt = "Rechte"
    Write-Step "Setze Rechte auf data\"
    Set-RelayDataAcl -Pfad $dataDir

    # -- ExchangeOnlineManagement (Fassung, Pruefsumme und Signatur fest) -------------
    # Nicht ueber Install-Module/PSGallery-Aufloesung, sondern das festgelegte nupkg: online
    # von der festen Adresse geladen, offline aus offline\module\ - in BEIDEN Faellen erst
    # SHA256 geprueft, dann jede Skriptdatei auf die Microsoft-Signatur (Install-RelayExoModul).
    # Nie beendend: ohne Modul laeuft das Relay, nur die Postfachabfrage fehlt.
    if (-not $SkipExoModule) {
        $schritt = "ExchangeOnlineManagement"
        $em = $RelayExoModul
        Write-Step "Pruefe PowerShell-Modul $($em.Name)"
        $modul = Get-Module -ListAvailable $em.Name | Sort-Object Version -Descending | Select-Object -First 1
        if ($modul) {
            Write-Ok "vorhanden: $($modul.Version)"
            if ("$($modul.Version)" -ne $em.Version) { Write-Ok "(erprobt ist $($em.Version); die vorhandene Fassung bleibt)" }
        } else {
            if ($RelayOffline) {
                $nupkg = Join-Path $quelle "offline\module\$($em.Datei)"
                $antwort = "j"
            } else {
                $nupkg = Join-Path $env:TEMP $em.Datei
                $antwort = if ($Unattended) { "j" } else { Read-Host "Modul fehlt. $($em.Name) $($em.Version) aus der PowerShell Gallery laden und pruefen? (j/n)" }
            }
            if ($antwort -match "^[jJyY]") {
                try {
                    if (-not $RelayOffline) { Get-RelayDownload $em.Url $nupkg }
                    $wohin = Install-RelayExoModul $nupkg
                    if (-not $RelayOffline) { Remove-Item $nupkg -ErrorAction SilentlyContinue }
                    Write-Ok "installiert: $($em.Name) $($em.Version) (Pruefsumme und Microsoft-Signatur geprueft) - $wohin"
                } catch {
                    Write-Warn "$($em.Name) nicht installiert: $($_.Exception.Message)"
                }
            } else {
                Write-Warn "Ohne das Modul gibt es keine Postfachabfrage - Adressen dann von Hand eintragen."
            }
        }
    }

    # -- Dienst registrieren ------------------------------------------------------------
    $schritt = "Dienst registrieren"
    Write-Step "Registriere Dienst ExoSmtpRelay"
    $service = Join-Path $InstallDir "windows\service.py"
    if ($dienst) {
        Invoke-RelayNativ "service.py update" { & $venvPython $service update } | Out-Null
    } else {
        Invoke-RelayNativ "service.py install" { & $venvPython $service --startup auto install } | Out-Null
    }
    $pyBasis = @(Invoke-RelayNativ "Python-Basis ermitteln" { & $venvPython -c "import sys; print(sys.base_prefix)" })[-1].Trim()
    Sync-RelayDienstDlls -Venv $venv -Basis $pyBasis -MaschinenPfad ([Environment]::GetEnvironmentVariable("Path", "Machine"))
    # Umgebung des Dienstes (Registry). pythonservice.exe bettet den Basis-Interpreter ein und
    # findet die Pakete der venv (servicemanager, pywin32) sonst nicht - Dienst startet nie.
    # ZUSAMMENFUEHREN statt ueberschreiben (windows\dienst_umgebung.py): eigene Eintraege wie
    # WEBUI_PASSWORD bleiben, ohne -WebPort bleibt der bisherige Port (8443 nur bei Erstinstallation).
    $sp = Join-Path $venv "Lib\site-packages"
    $umgebungArgs = @((Join-Path $InstallDir "windows\dienst_umgebung.py"), "--setzen", "PYTHONPATH=$sp;$sp\win32;$sp\win32\lib")
    if ($PSBoundParameters.ContainsKey("WebPort")) { $umgebungArgs += @("--port", "$WebPort") }
    $umgebungGesetzt = $true
    $umgebungAusgabe = @(Invoke-RelayNativ "Dienst-Umgebung (dienst_umgebung.py)" { & $venvPython @umgebungArgs })
    $WebPort = [int]($umgebungAusgabe[-1])
    Write-Ok "Web-Port: $WebPort"
    if ($istUpgrade) { Write-Ok "Dienst aktualisiert (Starttyp unveraendert)" } else { Write-Ok "Dienst registriert (Autostart)" }

    $schritt = "Firewall und Start"
    # -- Firewall ---------------------------------------------------------------------
    if (-not $SkipFirewall) {
        Write-Step "Firewall-Regeln"
        $firewallGesetzt = $true
        Sync-RelayFirewall -WebPort $WebPort
    }

    # -- Port 25 frei? -----------------------------------------------------------------
    $belegt = Get-NetTCPConnection -LocalPort 25 -State Listen -ErrorAction SilentlyContinue
    if ($belegt) {
        $prozess = Get-Process -Id $belegt[0].OwningProcess -ErrorAction SilentlyContinue
        Write-Warn "Port 25 ist bereits belegt (Prozess: $($prozess.ProcessName)). Der Dienst kann nicht starten, bis er frei ist."
    }

    # -- Web-Port frei? ------------------------------------------------------------------
    $belegtWeb = Get-NetTCPConnection -LocalPort $WebPort -State Listen -ErrorAction SilentlyContinue
    if ($belegtWeb) {
        $prozessWeb = Get-Process -Id $belegtWeb[0].OwningProcess -ErrorAction SilentlyContinue
        Write-Warn "Web-Port $WebPort ist bereits belegt (Prozess: $($prozessWeb.ProcessName)). Anderen Port waehlen: .\install.ps1 -WebPort <Port>"
    }

    # -- Starten ------------------------------------------------------------------------
    # -- Startmenue-Eintrag (alle Benutzer): oeffnet die Weboberflaeche ---------------
    Write-Step "Startmenue-Eintrag"
    $startmenue = [Environment]::GetFolderPath("CommonPrograms")
    $verknuepfung = Join-Path $startmenue "EXO SMTP Relay.url"
    # Rechnername statt localhost: das selbstsignierte TLS-Zertifikat des Dienstes traegt FQDN und kurzen
    # Rechnernamen als SAN (tls_cert.rechnernamen), nie "localhost" - der Link zeigt auf den FQDN.
    try { $rechnername = [System.Net.Dns]::GetHostEntry($env:COMPUTERNAME).HostName } catch { $rechnername = "" }
    if (-not $rechnername) { $rechnername = "localhost" }
    Set-Content -Path $verknuepfung -Encoding ASCII -Value @("[InternetShortcut]", "URL=https://${rechnername}:$WebPort/")
    Write-Ok "$verknuepfung"

    # Offene Ruecknahme des Updaters (data\update\ruecknahme_offen.json) gehoert zum Stand VOR
    # diesem Lauf - der Dienst-Wrapper holte sie beim Start nach und spielte damit eine aeltere
    # Fassung ueber die gerade installierte. Verwerfen; bricht dieser Lauf ab, kommt sie zurueck.
    $offenDatei = Join-Path $dataDir "update\ruecknahme_offen.json"
    if (Test-Path -LiteralPath $offenDatei) {
        $script:RelayOffenInhalt = [System.IO.File]::ReadAllText($offenDatei, [System.Text.Encoding]::UTF8)
        Remove-Item -LiteralPath $offenDatei -Force
        Write-Warn "Offene Ruecknahme des Updaters verworfen - diese Installation ersetzt den Stand vollstaendig"
    }
    Write-Step "Starte Dienst"
    Start-RelayDienst -DataDir $dataDir
    Write-Ok "Status: Running"
    $schritt = "Lebenszeichen"
    $neuVersion = (Get-Content (Join-Path $InstallDir "VERSION") -Raw).Trim()
    Wait-RelayGesund -Python $venvPython -Port $WebPort -Version $neuVersion
    Write-Ok "/health auf Port $WebPort meldet $neuVersion"
} catch {
    $grund = $_
    Write-Host ""
    Write-Warn "ABBRUCH im Schritt '$schritt': $($grund.Exception.Message)"
    Restore-RelayNachAbbruch
    throw "Installation abgebrochen im Schritt '$schritt': $($grund.Exception.Message)"
}
# Der Updater-Status zeigte nach verworfener offener Ruecknahme weiter "UNVOLLSTAENDIG" (Labtest
# 0.2.54, gelb) - diese Installation ist jetzt der Stand.
if ($script:RelayOffenInhalt) {
    try {
        $st = [ordered]@{ zustand = "ok"; text = "install.ps1 hat $((Get-Content (Join-Path $InstallDir 'VERSION') -Raw).Trim()) installiert; die offene Ruecknahme des Updaters ist damit erledigt"; zeit = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") }
        Write-RelayTextOhneBom (Join-Path $dataDir "update\status.json") (ConvertTo-Json -Compress -InputObject $st)
    } catch { Write-Warn "Status der Aktualisierung nicht zurueckgesetzt: $($_.Exception.Message)" }
}
if (Test-Path $sicherung) {
    # Erst den Merker: ohne ihn gilt ein Rest nie als Sicherung (Get-RelaySicherungsTeile, Ruecknahme).
    Remove-Item -LiteralPath (Join-Path $sicherung $RelayMerkerName) -Force -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $sicherung -ErrorAction SilentlyContinue
    if (Test-Path $sicherung) {
        # Labtest 0.2.49 Z2: Sperre in .install-alt\ - der Rest bleibt, ohne Merker gilt er nie als Sicherung.
        Write-Warn "Rest $sicherung nicht loeschbar (gesperrt?) - kein Fehler; der naechste Lauf entfernt ihn, sobald die Sperre weg ist"
    }
}

# Kennwort-Hinweis nach DERSELBEN Regel wie die Oberflaeche (app\kennwort_stand.py):
# nach einem Upgrade mit eigenem Kennwort kein "admin / admin" mehr (Labtest 0.2.43).
$kennwortStand = "unbekannt"
try {
    $kennwortStand = (& $venvPython (Join-Path $InstallDir "app\kennwort_stand.py") (Join-Path $dataDir "settings.json") | Select-Object -Last 1)
} catch { $kennwortStand = "unbekannt" }
if ($kennwortStand -eq "eigenes") {
    $kennwortHinweis = "Kennwort unveraendert"
} elseif ($kennwortStand -eq "vorgabe") {
    $kennwortHinweis = "admin / admin - bitte aendern"
} else {
    $kennwortHinweis = "Kennwort: falls noch admin / admin, bitte aendern"
}

Write-Host ""
if ($istUpgrade) { $fertig = "Aktualisiert" } else { $fertig = "Fertig" }
Write-Host "${fertig}. Weboberflaeche: https://${rechnername}:$WebPort  ($kennwortHinweis; Startmenue: EXO SMTP Relay)" -ForegroundColor Green
Write-Host "Protokoll: $InstallDir\data\logs\app.log"
Write-Host "Entfernen: .\uninstall.ps1"
