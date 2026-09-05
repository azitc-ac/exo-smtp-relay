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
    Port der Weboberflaeche. Vorgabe: 8080

.PARAMETER SkipFirewall
    Keine Firewall-Regeln anlegen.

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
    [int]$WebPort = 8080,
    [switch]$SkipFirewall,
    [switch]$SkipExoModule
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "    OK: $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "    WARNUNG: $msg" -ForegroundColor Yellow }

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

# -- Python finden ------------------------------------------------------------------
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
    throw "Kein Python 3.11+ gefunden. Bitte von https://www.python.org/downloads/windows/ installieren (Haken 'Add python.exe to PATH') oder -PythonExe angeben."
}
Write-Ok "Python: $PythonExe"

# -- Dienst anhalten, falls vorhanden -------------------------------------------
$dienst = Get-Service -Name "ExoSmtpRelay" -ErrorAction SilentlyContinue
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
foreach ($datei in @("VERSION", "CHANGELOG.md", "LICENSE.md", "README.md")) {
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
$acl = Get-Acl $dataDir
$acl.SetAccessRuleProtection($true, $false)
foreach ($wer in @("NT AUTHORITY\SYSTEM", "BUILTIN\Administrators")) {
    $regel = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $wer, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")
    $acl.AddAccessRule($regel)
}
Set-Acl $dataDir $acl
Write-Ok "data\ nur fuer SYSTEM und Administratoren lesbar"

# -- ExchangeOnlineManagement ---------------------------------------------------------
if (-not $SkipExoModule) {
    Write-Step "Pruefe PowerShell-Modul ExchangeOnlineManagement"
    $modul = Get-Module -ListAvailable ExchangeOnlineManagement | Sort-Object Version -Descending | Select-Object -First 1
    if ($modul) {
        Write-Ok "vorhanden: $($modul.Version)"
    } else {
        $antwort = Read-Host "Modul fehlt. Jetzt aus der PowerShell Gallery installieren? (j/n)"
        if ($antwort -match "^[jJyY]") {
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
# WEBUI_PORT als Umgebungsvariable des Dienstes (Registry), wenn abweichend
if ($WebPort -ne 8080) {
    $regPfad = "HKLM:\SYSTEM\CurrentControlSet\Services\ExoSmtpRelay"
    New-ItemProperty -Path $regPfad -Name "Environment" -PropertyType MultiString `
        -Value @("WEBUI_PORT=$WebPort") -Force | Out-Null
}
Write-Ok "Dienst registriert (Autostart)"

# -- Firewall ---------------------------------------------------------------------
if (-not $SkipFirewall) {
    Write-Step "Firewall-Regeln"
    foreach ($regel in @(@{ Name = "EXO SMTP Relay - SMTP 25"; Port = 25 },
                         @{ Name = "EXO SMTP Relay - Web $WebPort"; Port = $WebPort })) {
        if (-not (Get-NetFirewallRule -DisplayName $regel.Name -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $regel.Name -Direction Inbound -Protocol TCP `
                -LocalPort $regel.Port -Action Allow -Profile Domain,Private | Out-Null
        }
    }
    Write-Ok "Port 25 und $WebPort eingehend (Domaene/Privat)"
}

# -- Port 25 frei? -----------------------------------------------------------------
$belegt = Get-NetTCPConnection -LocalPort 25 -State Listen -ErrorAction SilentlyContinue
if ($belegt) {
    $prozess = Get-Process -Id $belegt[0].OwningProcess -ErrorAction SilentlyContinue
    Write-Warn "Port 25 ist bereits belegt (Prozess: $($prozess.ProcessName)). Der Dienst kann nicht starten, bis er frei ist."
}

# -- Starten ------------------------------------------------------------------------
Write-Step "Starte Dienst"
Start-Service -Name "ExoSmtpRelay"
Start-Sleep -Seconds 3
$dienst = Get-Service -Name "ExoSmtpRelay"
Write-Ok "Status: $($dienst.Status)"

Write-Host ""
Write-Host "Fertig. Weboberflaeche: https://localhost:$WebPort  (admin / admin - bitte aendern)" -ForegroundColor Green
Write-Host "Protokoll: $InstallDir\data\logs\app.log"
Write-Host "Entfernen: .\uninstall.ps1"
