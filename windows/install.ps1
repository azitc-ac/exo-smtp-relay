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
      * Python 3.11 oder neuer (https://www.python.org/downloads/windows/,
        "Add python.exe to PATH" anhaken) - oder -PythonExe angeben
      * Fuer die Postfachabfrage: Install-Module ExchangeOnlineManagement
        (der Installer bietet es an)

    Laeuft unter Windows PowerShell 5.1 und PowerShell 7.

.PARAMETER InstallDir
    Zielverzeichnis. Vorgabe: C:\ProgramData\exo-smtp-relay

.PARAMETER PythonExe
    Pfad zu python.exe. Ohne Angabe wird "py -3" bzw. "python" gesucht.

.PARAMETER WebPort
    Port der Weboberflaeche. Vorgabe: 8443 bei der Erstinstallation; bei einem Upgrade
    ohne Angabe bleibt der bisherige Port.

.PARAMETER SkipFirewall
    Keine Firewall-Regeln anlegen.

.PARAMETER Unattended
    Keine Rueckfragen: Python und ExchangeOnlineManagement werden ohne Nachfrage installiert.

.PARAMETER SkipExoModule
    ExchangeOnlineManagement nicht installieren/pruefen.

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
    $null = & icacls.exe $Pfad /reset /C /Q 2>&1
    return ($LASTEXITCODE -eq 0)
}

function Set-RelayDataAcl([string]$Pfad) {
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
        if (Reset-RelayAclErbe $o.FullName) { $zurueck++ } else { Write-Warn "nicht zurueckgesetzt: $name (icacls `"$($o.FullName)`" /reset)"; $probleme++ }
    }
    if ($probleme -eq 0) {
        Write-Ok "data\ nur fuer SYSTEM und Administratoren lesbar ($entfernt fremde Eintraege entfernt, $zurueck Unterobjekte zurueckgesetzt)"
    } else {
        Write-Warn "data\ auf SYSTEM und Administratoren gesetzt, aber $probleme Unterobjekt(e) weichen ab - siehe oben"
    }
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

# -- Python finden oder installieren ------------------------------------------------
Write-Step "Suche Python 3.11+"
if ($PythonExe -eq "") {
    $kandidaten = @()
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $kandidaten += @{ Exe = $py.Source; Args = @("-3") } }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { $kandidaten += @{ Exe = $python.Source; Args = @() } }
    foreach ($k in $kandidaten) {
        try {
            $ver = & $k.Exe @($k.Args + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")) 2>$null
            if ($ver -and ([version]$ver -ge [version]"3.11")) {
                $PythonExe = (& $k.Exe @($k.Args + @("-c", "import sys; print(sys.executable)"))).Trim()
                break
            }
        } catch { }
    }
}

if ($PythonExe -eq "" -or -not (Test-Path $PythonExe)) {
    Write-Warn "Python 3.11+ nicht gefunden."
    $antwort = if ($Unattended) { "j" } else { Read-Host "Jetzt herunterladen und installieren? (j/n)" }
    if ($antwort -match "^[jJyY]") {
        Write-Step "Lade Python 3.12 herunter"
        $pythonZip = "$env:TEMP\python-3.11-windows.zip"
        $pythonMsi = "$env:TEMP\python-installer.exe"

        # Nutzer zur Python.org-Seite fuehren oder automatisch MSI runterladen
        # Hier: directed install von der aktuellsten stabilen Version
        $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
        $url = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-$arch.exe"

        try {
            Write-Host "    Lade herunter: $url" -ForegroundColor Gray
            $ProgressPreference = 'SilentlyContinue'
            Invoke-WebRequest -Uri $url -OutFile $pythonMsi -ErrorAction Stop

            Write-Step "Installiere Python 3.12"
            & $pythonMsi /quiet InstallAllUsers=1 PrependPath=1 | Out-Null
            Remove-Item $pythonMsi -ErrorAction SilentlyContinue

            # Nach Installation neuerlich versuchen
            $py = Get-Command py -ErrorAction SilentlyContinue
            if ($py) {
                $PythonExe = (& $py.Source @("-3", "-c", "import sys; print(sys.executable)")).Trim()
            }
            if ($PythonExe -and (Test-Path $PythonExe)) {
                Write-Ok "Python installiert: $PythonExe"
            } else {
                throw "Python-Installation fehlgeschlagen oder nicht auffindbar"
            }
        } catch {
            Write-Host ""
            Write-Host "Automatischer Download fehlgeschlagen." -ForegroundColor Yellow
            Write-Host "Bitte von https://www.python.org/downloads/windows/ installieren:" -ForegroundColor Yellow
            Write-Host "  * Python 3.11 oder neuer waehlen" -ForegroundColor Yellow
            Write-Host "  * Haken bei 'Add python.exe to PATH' setzen" -ForegroundColor Yellow
            Write-Host "  * Installation abschliessen und dann dieses Skript erneut ausfuehren" -ForegroundColor Yellow
            throw "Python erforderlich. Siehe obige Anleitung."
        }
    } else {
        throw "Python 3.11+ erforderlich. Bitte von https://www.python.org/downloads/windows/ installieren (Haken 'Add python.exe to PATH')."
    }
}
Write-Ok "Python: $PythonExe"

# -- Dienst anhalten, falls vorhanden -------------------------------------------
$dienst = Get-Service -Name "ExoSmtpRelay" -ErrorAction SilentlyContinue
$istUpgrade = [bool]$dienst
if ($dienst -and $dienst.Status -eq "Running") {
    Write-Step "Halte laufenden Dienst an"
    Stop-Service -Name "ExoSmtpRelay" -Force
    Write-Ok "angehalten"
}

# -- Dateien kopieren ---------------------------------------------------------------
Write-Step "Kopiere Anwendung nach $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "data") | Out-Null
foreach ($teil in @("app", "windows")) {
    $ziel = Join-Path $InstallDir $teil
    if (Test-Path $ziel) { Remove-Item -Recurse -Force $ziel }
    Copy-Item -Recurse -Force (Join-Path $quelle $teil) $ziel
}
foreach ($datei in @("VERSION", "CHANGELOG.md", "LICENSE.md", "NOTICE", "README.md")) {
    $q = Join-Path $quelle $datei
    if (Test-Path $q) { Copy-Item -Force $q (Join-Path $InstallDir $datei) }
}
# __pycache__ aus der Quelle nicht mitschleppen
Get-ChildItem -Path (Join-Path $InstallDir "app") -Recurse -Directory -Filter "__pycache__" |
    ForEach-Object { Remove-Item -Recurse -Force $_.FullName }
Write-Ok "kopiert"

# -- venv und Abhaengigkeiten -------------------------------------------------------
$venv = Join-Path $InstallDir "venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Step "Erstelle virtuelle Umgebung"
    & $PythonExe -m venv $venv
    Write-Ok "venv: $venv"
}
Write-Step "Installiere Abhaengigkeiten (requirements.lock)"
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r (Join-Path $InstallDir "app\requirements.lock")
# pywin32: Nachinstallation registriert die Dienst-DLLs
$post = Join-Path $venv "Scripts\pywin32_postinstall.py"
if (Test-Path $post) { & $venvPython $post -install -silent | Out-Null }
Write-Ok "Abhaengigkeiten installiert"

# -- Datenverzeichnis absichern: nur SYSTEM und Administratoren ----------------
Write-Step "Setze Rechte auf data\"
$dataDir = Join-Path $InstallDir "data"
Set-RelayDataAcl -Pfad $dataDir

# -- ExchangeOnlineManagement ---------------------------------------------------------
if (-not $SkipExoModule) {
    Write-Step "Pruefe PowerShell-Modul ExchangeOnlineManagement"
    $modul = Get-Module -ListAvailable ExchangeOnlineManagement | Sort-Object Version -Descending | Select-Object -First 1
    if ($modul) {
        Write-Ok "vorhanden: $($modul.Version)"
    } else {
        $antwort = if ($Unattended) { "j" } else { Read-Host "Modul fehlt. Jetzt aus der PowerShell Gallery installieren? (j/n)" }
        if ($antwort -match "^[jJyY]") {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force -Scope AllUsers | Out-Null
            Install-Module ExchangeOnlineManagement -Scope AllUsers -Force -AllowClobber
            Write-Ok "installiert"
        } else {
            Write-Warn "Ohne das Modul gibt es keine Postfachabfrage - Adressen dann von Hand eintragen."
        }
    }
}

# -- Dienst registrieren ------------------------------------------------------------
Write-Step "Registriere Dienst ExoSmtpRelay"
$service = Join-Path $InstallDir "windows\service.py"
if ($dienst) {
    & $venvPython $service update | Out-Null
} else {
    & $venvPython $service --startup auto install | Out-Null
}
# Umgebung des Dienstes (Registry). pythonservice.exe bettet den Basis-Interpreter ein und
# findet die Pakete der venv (servicemanager, pywin32) sonst nicht - Dienst startet nie.
# ZUSAMMENFUEHREN statt ueberschreiben (windows\dienst_umgebung.py): eigene Eintraege wie
# WEBUI_PASSWORD bleiben, ohne -WebPort bleibt der bisherige Port (8443 nur bei Erstinstallation).
$sp = Join-Path $venv "Lib\site-packages"
$umgebungArgs = @((Join-Path $InstallDir "windows\dienst_umgebung.py"), "--setzen", "PYTHONPATH=$sp;$sp\win32;$sp\win32\lib")
if ($PSBoundParameters.ContainsKey("WebPort")) { $umgebungArgs += @("--port", "$WebPort") }
$umgebungAusgabe = @(& $venvPython @umgebungArgs)
if ($LASTEXITCODE -ne 0) { throw "Dienst-Umgebung nicht gesetzt: $($umgebungAusgabe -join ' ')" }
$WebPort = [int]($umgebungAusgabe[-1])
Write-Ok "Web-Port: $WebPort"
if ($istUpgrade) { Write-Ok "Dienst aktualisiert (Starttyp unveraendert)" } else { Write-Ok "Dienst registriert (Autostart)" }

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
Write-Ok "Status: $($dienst.Status)"

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
