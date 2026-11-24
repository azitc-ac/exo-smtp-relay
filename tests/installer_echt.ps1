<#
.SYNOPSIS
    Echter Durchlauf von windows\install.ps1 auf einem Windows-Rechner (CI-Job "Installer echt").

.DESCRIPTION
    Nur fuer die CI bzw. eine Wegwerf-VM: installiert den Dienst ExoSmtpRelay wirklich
    (C:\ProgramData\exo-smtp-relay), braucht Administratorrechte und Python 3.11+.

      1. Erstinstallation                      -> Exit 0, Dienst laeuft, /health meldet VERSION
      2. Gegenprobe 0.2.45 (Labtest, Fall c2)   -> Datei in data\ mit Eigentuemer SYSTEM und nur
                                                 SYSTEM:F: install.ps1 von 0.2.45 bricht ab, Dienst
                                                 bleibt gestoppt (der Fehler, den 0.2.46 behebt)
      3. Upgrade mit derselben Datei            -> Exit 0, WARNUNG, Dienst laeuft
      4. Upgrade, das an pip scheitert          -> Exit <> 0, vorige Fassung zurueck, Dienst laeuft
                                                 wieder mit der alten VERSION, .install-alt\ weg

    -Gegenprobe gibt den Pfad eines entpackten 0.2.45-Baums an (CI: git worktree).
#>
param(
    [string]$Quelle = (Split-Path -Parent $PSScriptRoot),
    [string]$Gegenprobe = ""
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ziel = "C:\ProgramData\exo-smtp-relay"
$arbeit = Join-Path $env:RUNNER_TEMP "installer-echt"
if (-not $env:RUNNER_TEMP) { $arbeit = Join-Path $env:TEMP "installer-echt" }
New-Item -ItemType Directory -Force -Path $arbeit | Out-Null

function Pruefe([bool]$Bedingung, [string]$Text) {
    if (-not $Bedingung) { throw "FEHLGESCHLAGEN: $Text" }
    Write-Host "  OK  $Text" -ForegroundColor Green
}

function Install-Lauf([string]$Baum, [string]$Name) {
    Write-Host "`n=== $Name ===" -ForegroundColor Cyan
    $log = Join-Path $arbeit "$Name.log"
    # Eigene Windows-PowerShell-5.1-Sitzung wie beim Betreiber; PSModulePath einer pwsh-Sitzung
    # wuerde sonst PS7-Module in 5.1 laden.
    $alt = $env:PSModulePath
    $env:PSModulePath = ""
    try {
        $p = Start-Process -FilePath "powershell.exe" -Wait -PassThru -NoNewWindow `
            -RedirectStandardOutput $log -RedirectStandardError "$log.err" -ArgumentList @(
                "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                (Join-Path $Baum "windows\install.ps1"), "-Unattended", "-SkipExoModule")
    } finally { $env:PSModulePath = $alt }
    $text = (Get-Content $log -Raw -ErrorAction SilentlyContinue) + "`n" + (Get-Content "$log.err" -Raw -ErrorAction SilentlyContinue)
    Write-Host $text
    return @{ Rc = $p.ExitCode; Text = $text }
}

function Get-Gesundheit {
    for ($i = 0; $i -lt 40; $i++) {
        $roh = & curl.exe -sk --max-time 5 "https://localhost:8443/health" 2>$null
        if ($LASTEXITCODE -eq 0 -and $roh) { try { return ($roh | ConvertFrom-Json) } catch { } }
        Start-Sleep -Seconds 3
    }
    return $null
}

function Get-Fassung { (Get-Content (Join-Path $ziel "VERSION") -Raw).Trim() }
function Get-DienstStatus { "$((Get-Service -Name ExoSmtpRelay -ErrorAction SilentlyContinue).Status)" }

function New-Baum([string]$Name) {
    $b = Join-Path $arbeit $Name
    if (Test-Path $b) { Remove-Item -Recurse -Force $b }
    New-Item -ItemType Directory -Path $b | Out-Null
    foreach ($t in @("app", "windows", "VERSION", "CHANGELOG.md", "LICENSE.md", "NOTICE", "README.md")) {
        Copy-Item -Recurse -Force (Join-Path $Quelle $t) (Join-Path $b $t)
    }
    return $b
}

$version = (Get-Content (Join-Path $Quelle "VERSION") -Raw).Trim()

# 1 -- Erstinstallation ----------------------------------------------------------------
$r = Install-Lauf (New-Baum "q1") "1-erstinstallation"
Pruefe ($r.Rc -eq 0) "Erstinstallation: Exit 0 (ist $($r.Rc))"
Pruefe ((Get-DienstStatus) -eq "Running") "Dienst laeuft"
$g = Get-Gesundheit
Pruefe ($null -ne $g -and $g.version -eq $version) "/health meldet $version"

# Fall c2: Datei, die der Administrator nicht zuruecksetzen darf --------------------------
$gesperrt = Join-Path $ziel "data\gesperrt.json"
Set-Content -Path $gesperrt -Value "{}"
& icacls.exe $gesperrt /setowner "*S-1-5-18" | Out-Null
Pruefe ($LASTEXITCODE -eq 0) "c2: Eigentuemer SYSTEM gesetzt"
& icacls.exe $gesperrt /inheritance:r /grant:r "*S-1-5-18:F" | Out-Null
Pruefe ($LASTEXITCODE -eq 0) "c2: nur SYSTEM:F"

# 2 -- Gegenprobe mit 0.2.45 ---------------------------------------------------------------
if ($Gegenprobe) {
    $r = Install-Lauf $Gegenprobe "2-gegenprobe-0245"
    Pruefe ($r.Rc -ne 0) "Gegenprobe: install.ps1 von 0.2.45 bricht an der gesperrten Datei ab (Exit $($r.Rc))"
    Pruefe ((Get-DienstStatus) -ne "Running") "Gegenprobe: Dienst bleibt gestoppt (der Labtest-Fehler)"
}

# 3 -- Upgrade mit gesperrter Datei --------------------------------------------------------
$r = Install-Lauf (New-Baum "q1") "3-upgrade-c2"
Pruefe ($r.Rc -eq 0) "Upgrade mit gesperrter Datei: Exit 0 (ist $($r.Rc))"
Pruefe ($r.Text -match "WARNUNG: nicht zurueckgesetzt: gesperrt\.json \(Exit [1-9]" -or $r.Text -match "WARNUNG: Rechte nicht lesbar: gesperrt\.json") "WARNUNG zur gesperrten Datei"
Pruefe ((Get-DienstStatus) -eq "Running") "Dienst laeuft"
$g = Get-Gesundheit
Pruefe ($null -ne $g -and $g.version -eq $version) "/health meldet $version"

# 4 -- Upgrade, das an pip scheitert: Ruecknahme ---------------------------------------------
$q2 = New-Baum "q2"
Set-Content -Path (Join-Path $q2 "VERSION") -Value "9.9.9" -NoNewline
Add-Content -Path (Join-Path $q2 "app\requirements.lock") -Value "gibt-es-nicht-exo-relay-ci==0.0.1"
$r = Install-Lauf $q2 "4-abbruch"
Pruefe ($r.Rc -ne 0) "Kaputtes Upgrade: Exit <> 0 (ist $($r.Rc))"
Pruefe ($r.Text -match "ABBRUCH im Schritt 'Abhaengigkeiten'") "Abbruch im Schritt Abhaengigkeiten gemeldet"
Pruefe ($r.Text -match "Vorige Fassung $([regex]::Escape($version)) zurueckgeholt") "vorige Fassung zurueckgeholt"
Pruefe ($r.Text -match "Dienst laeuft wieder \(Fassung $([regex]::Escape($version))\)") "Dienst laeuft wieder gemeldet"
Pruefe ((Get-Fassung) -eq $version) "installierte VERSION wieder $version"
Pruefe ((Get-DienstStatus) -eq "Running") "Dienst laeuft"
Pruefe (-not (Test-Path (Join-Path $ziel ".install-alt"))) ".install-alt\ aufgeraeumt"
$g = Get-Gesundheit
Pruefe ($null -ne $g -and $g.version -eq $version) "/health meldet wieder $version"

Write-Host "`nAlle Pruefungen bestanden." -ForegroundColor Green
