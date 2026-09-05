<#
.SYNOPSIS
    Entfernt den Windows-Dienst des EXO SMTP Relay.

.DESCRIPTION
    Haelt den Dienst an, hebt die Registrierung auf und entfernt die
    Firewall-Regeln. Das Installationsverzeichnis samt data\ (Einstellungen,
    Geraeteliste, Zertifikate) bleibt erhalten, sofern nicht -RemoveData
    angegeben ist.

.PARAMETER InstallDir
    Installationsverzeichnis. Vorgabe: C:\ProgramData\exo-smtp-relay

.PARAMETER RemoveData
    Auch das Installationsverzeichnis mit allen Daten loeschen.
#>
param(
    [string]$InstallDir = "C:\ProgramData\exo-smtp-relay",
    [switch]$RemoveData
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "    OK: $msg" -ForegroundColor Green }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Bitte als Administrator ausfuehren."
}

$venvPython = Join-Path $InstallDir "venv\Scripts\python.exe"
$service = Join-Path $InstallDir "windows\service.py"

$dienst = Get-Service -Name "ExoSmtpRelay" -ErrorAction SilentlyContinue
if ($dienst) {
    Write-Step "Halte Dienst an und entferne ihn"
    if ($dienst.Status -eq "Running") { Stop-Service -Name "ExoSmtpRelay" -Force }
    if ((Test-Path $venvPython) -and (Test-Path $service)) {
        & $venvPython $service remove | Out-Null
    } else {
        & sc.exe delete ExoSmtpRelay | Out-Null
    }
    Write-Ok "Dienst entfernt"
} else {
    Write-Ok "Kein Dienst ExoSmtpRelay registriert"
}

Write-Step "Firewall-Regeln"
Get-NetFirewallRule -DisplayName "EXO SMTP Relay - *" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Ok "entfernt"

if ($RemoveData) {
    Write-Step "Loesche $InstallDir"
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    Write-Ok "geloescht"
} else {
    Write-Host ""
    Write-Host "Daten bleiben unter $InstallDir\data (Einstellungen, Geraeteliste, Zertifikate)."
    Write-Host "Vollstaendig entfernen: .\uninstall.ps1 -RemoveData"
}
