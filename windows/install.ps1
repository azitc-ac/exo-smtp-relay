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
        foreach ($r in $eigene) { Write-Ok "entfernt auf ${name}: $(Get-RelayKontoName $r.IdentityReference) $($r.AccessControlType) $($r.FileSystemRights)" }
        if ($geschuetzt) { Write-Ok "Vererbung wieder an: $name" }
        if (Reset-RelayAclErbe $o.FullName) { $zurueck++ } else { Write-Warn "nicht zurueckgesetzt: $name ($($script:RelayIcaclsMeldung); von Hand: icacls `"$($o.FullName)`" /reset)"; $probleme++ }
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
# Rueckgabe: die verschobenen Teile.
function Backup-RelayFassung([string]$Ziel, [string]$Sicherung, [string[]]$Teile) {
    if (Test-Path -LiteralPath $Sicherung) {
        Write-Warn "Rest eines frueheren Laufs entfernt: $Sicherung"
        Remove-Item -LiteralPath $Sicherung -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Sicherung | Out-Null
    foreach ($teil in $Teile) {
        $p = Join-Path $Ziel $teil
        if (Test-Path -LiteralPath $p) {
            Move-Item -LiteralPath $p -Destination (Join-Path $Sicherung $teil)
            $teil
        }
    }
}

# Gegenstueck: gesicherte Teile zurueck an ihren Platz, das Neue davor weg.
function Restore-RelayFassung([string]$Ziel, [string]$Sicherung, [string[]]$Teile) {
    foreach ($teil in $Teile) {
        $alt = Join-Path $Sicherung $teil
        if (-not (Test-Path -LiteralPath $alt)) { continue }
        $p = Join-Path $Ziel $teil
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
        Move-Item -LiteralPath $alt -Destination $p
    }
    Remove-Item -LiteralPath $Sicherung -Recurse -Force -ErrorAction SilentlyContinue
}

# Aus dem catch des Hauptablaufs: vorige Fassung zurueck, Abhaengigkeiten zurueck, Dienst wieder
# starten, wenn er vorher lief. Liest die Ablaufvariablen ($gesichert, $pipGelaufen ...).
function Restore-RelayNachAbbruch {
    try { Stop-Service -Name "ExoSmtpRelay" -Force -ErrorAction SilentlyContinue } catch { }
    if (@($gesichert).Count -gt 0) {
        try {
            Restore-RelayFassung -Ziel $InstallDir -Sicherung $sicherung -Teile $gesichert
            Write-Warn "Vorige Fassung $altVersion zurueckgeholt ($($gesichert -join ', '))"
        } catch {
            Write-Warn "Ruecknahme unvollstaendig: $($_.Exception.Message) - Sicherung unter $sicherung"
        }
        $lock = Join-Path $InstallDir "app\requirements.lock"
        if ($pipGelaufen -and (Test-Path $venvPython) -and (Test-Path $lock)) {
            try {
                $pipArgs = @(Get-RelayPipArgumente -InstallDir $InstallDir -Lock $lock)
                Invoke-RelayNativ "pip (Ruecknahme)" { & $venvPython @pipArgs } | Out-Null
                Write-Warn "Abhaengigkeiten auf Fassung $altVersion zurueckgesetzt"
            } catch {
                Write-Warn "Abhaengigkeiten nicht zurueckgesetzt: $($_.Exception.Message)"
            }
        }
    } elseif ($istUpgrade) {
        Write-Warn "Abbruch vor dem Austausch - die installierte Fassung ist unveraendert."
    } else {
        Write-Warn "Erstinstallation unvollstaendig - Ursache beheben und install.ps1 erneut ausfuehren."
    }
    if ($dienstLief) {
        try {
            Start-Service -Name "ExoSmtpRelay"
            $v = ""
            if (Test-Path (Join-Path $InstallDir "VERSION")) { $v = (Get-Content (Join-Path $InstallDir "VERSION") -Raw).Trim() }
            Write-Warn "Dienst laeuft wieder (Fassung $v)"
        } catch {
            Write-Warn "Dienst startet nicht: $($_.Exception.Message) - Protokoll: $dataDir\logs\app.log"
        }
    }
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
    if (Test-Path -LiteralPath $ziel) { Remove-Item -LiteralPath $ziel -Recurse -Force }
    Move-Item -LiteralPath $tmp -Destination $ziel
    return $ziel
}

# pip-Argumente - DIESELBEN wie app\update_tausch.pip_argumente (Updater): nur Wheels, nur mit
# den Pruefsummen der Lock-Datei; liegt offline\wheels in der Installation, ohne Index daraus.
# Gleichheit prueft tests\test_abhaengigkeiten.py.
function Get-RelayPipArgumente([string]$InstallDir, [string]$Lock) {
    $a = @("-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--require-hashes", "--only-binary", ":all:")
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
$pipGelaufen = $false
$schritt = "Kopieren"
try {
    # -- Dateien kopieren -----------------------------------------------------------
    Write-Step "Kopiere Anwendung nach $InstallDir"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    $gesichert = @(Backup-RelayFassung -Ziel $InstallDir -Sicherung $sicherung -Teile $RelayTeile)
    foreach ($teil in $RelayTeile) {
        $q = Join-Path $quelle $teil
        if ($teil -eq "offline") { continue }
        if (Test-Path $q) { Copy-Item -Recurse -Force $q (Join-Path $InstallDir $teil) }
    }
    # offline\: nur Wheels und inhalt.json (der Updater installiert daraus). Installer und
    # Modulpaket bleiben im Paket. Eine Online-Installation hat keins - war vorher eins da,
    # liegt es jetzt in .install-alt\ und verschwindet mit ihr.
    if ($RelayOffline) {
        $oz = Join-Path $InstallDir "offline"
        New-Item -ItemType Directory -Force -Path $oz | Out-Null
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
    $umgebungAusgabe = @(Invoke-RelayNativ "Dienst-Umgebung (dienst_umgebung.py)" { & $venvPython @umgebungArgs })
    $WebPort = [int]($umgebungAusgabe[-1])
    Write-Ok "Web-Port: $WebPort"
    if ($istUpgrade) { Write-Ok "Dienst aktualisiert (Starttyp unveraendert)" } else { Write-Ok "Dienst registriert (Autostart)" }

    $schritt = "Firewall und Start"
    # -- Firewall ---------------------------------------------------------------------
    if (-not $SkipFirewall) {
        Write-Step "Firewall-Regeln"
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

    Write-Step "Starte Dienst"
    Start-Service -Name "ExoSmtpRelay"
    Start-Sleep -Seconds 3
    $dienst = Get-Service -Name "ExoSmtpRelay"
    if ($dienst.Status -ne "Running") { throw "Dienst laeuft nach dem Start nicht (Status: $($dienst.Status)) - Protokoll: $dataDir\logs\app.log" }
    Write-Ok "Status: $($dienst.Status)"
} catch {
    $grund = $_
    Write-Host ""
    Write-Warn "ABBRUCH im Schritt '$schritt': $($grund.Exception.Message)"
    Restore-RelayNachAbbruch
    throw "Installation abgebrochen im Schritt '$schritt': $($grund.Exception.Message)"
}
if (Test-Path $sicherung) { Remove-Item -Recurse -Force $sicherung -ErrorAction SilentlyContinue }

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
