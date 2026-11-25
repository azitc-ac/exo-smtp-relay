<#
.SYNOPSIS
    Echter Durchlauf von windows\install.ps1 auf einem Windows-Rechner (CI-Job "Installer echt").

.DESCRIPTION
    Nur fuer die CI bzw. eine Wegwerf-VM: installiert den Dienst ExoSmtpRelay wirklich
    (C:\ProgramData\exo-smtp-relay), braucht Administratorrechte und Python 3.11+.

      1. Erstinstallation                      -> Exit 0, Dienst laeuft, /health meldet VERSION
      2. Upgrade mit gesperrter Datei (c2)      -> Exit 0, WARNUNG mit Exit 5 "Access is denied",
                                                 Dienst laeuft
      3. Gegenprobe 0.2.45 (die Labtest-Fehler): mit der gesperrten Datei Abbruch an "Access is
         denied", Dienst gestoppt, VERSION schon neu; ein an pip scheiterndes Upgrade bricht
         ebenso ab und laesst VERSION 9.9.9 und einen gestoppten Dienst zurueck
      4. Wiederherstellen                        -> Exit 0, Dienst laeuft
      5. An pip scheiterndes Upgrade             -> Exit <> 0, vorige Fassung zurueck, Dienst laeuft
                                                 wieder mit der alten VERSION, .install-alt\ weg

      6. windows\ gesperrt (Arbeitsverzeichnis eines Prozesses), zweimal -> Exit <> 0, "nichts
         verschoben", app\ bleibt, Dienst laeuft; Gegenprobe 2ea9192: app\ verschwindet
      7. neue Fassung mit kaputter main.py und -WebPort 9443 -> Ruecknahme, /health alte Fassung,
         Port 8443 in Dienst-Umgebung und Firewall; Gegenprobe 2ea9192: "Aktualisiert", /health stumm

    -Gegenprobe046 gibt den Pfad eines 2ea9192-Baums an (0.2.46, vom Labtest gestoppt).
    -Gegenprobe gibt den Pfad eines entpackten 0.2.45-Baums an (CI: git worktree).
#>
param(
    [string]$Quelle = (Split-Path -Parent $PSScriptRoot),
    [string]$Gegenprobe = "",
    [string]$Gegenprobe046 = ""
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ziel = "C:\ProgramData\exo-smtp-relay"
$arbeit = Join-Path $env:RUNNER_TEMP "installer-echt"
if (-not $env:RUNNER_TEMP) { $arbeit = Join-Path $env:TEMP "installer-echt" }
New-Item -ItemType Directory -Force -Path $arbeit | Out-Null

function Pruefe([bool]$Bedingung, [string]$Text) {
    if (-not $Bedingung) { Show-Diagnose; throw "FEHLGESCHLAGEN: $Text" }
    Write-Host "  OK  $Text" -ForegroundColor Green
}

function Install-Lauf([string]$Baum, [string]$Name, [string[]]$Zusatz = @()) {
    Write-Host "`n=== $Name ===" -ForegroundColor Cyan
    $log = Join-Path $arbeit "$Name.log"
    # Eigene Windows-PowerShell-5.1-Sitzung wie beim Betreiber; PSModulePath einer pwsh-Sitzung
    # wuerde sonst PS7-Module in 5.1 laden.
    $alt = $env:PSModulePath
    $env:PSModulePath = ""
    try {
        $p = Start-Process -FilePath "powershell.exe" -Wait -PassThru -NoNewWindow `
            -RedirectStandardOutput $log -RedirectStandardError "$log.err" -ArgumentList (@(
                "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                (Join-Path $Baum "windows\install.ps1"), "-Unattended", "-SkipExoModule") + $Zusatz)
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

function Show-Diagnose {
    Write-Host "`n--- Diagnose ---" -ForegroundColor Yellow
    & sc.exe qc ExoSmtpRelay
    Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\ExoSmtpRelay" -ErrorAction SilentlyContinue | Format-List ImagePath, Environment
    foreach ($f in @("service.log", "app.log")) {
        $l = Join-Path $ziel "data\logs\$f"
        if (Test-Path $l) { Write-Host "--- $f"; Get-Content $l -Tail 40 }
    }
    Get-WinEvent -FilterHashtable @{ LogName = "System"; ProviderName = "Service Control Manager"; StartTime = (Get-Date).AddMinutes(-15) } -MaxEvents 10 -ErrorAction SilentlyContinue |
        Format-List TimeCreated, Id, Message
    Get-WinEvent -FilterHashtable @{ LogName = "Application"; StartTime = (Get-Date).AddMinutes(-15) } -MaxEvents 10 -ErrorAction SilentlyContinue |
        Format-List TimeCreated, ProviderName, Id, Message
    $cfg = Join-Path $ziel "venv\pyvenv.cfg"
    if (Test-Path $cfg) { Get-Content $cfg }
    Write-Host "Maschinen-PATH: $([Environment]::GetEnvironmentVariable('Path', 'Machine'))"
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

# 2 -- Upgrade mit gesperrter Datei (Fall c2) ------------------------------------------------
$gesperrt = Join-Path $ziel "data\gesperrt.json"
Set-Content -Path $gesperrt -Value "{}"
& icacls.exe $gesperrt /setowner "*S-1-5-18" | Out-Null
Pruefe ($LASTEXITCODE -eq 0) "c2: Eigentuemer SYSTEM gesetzt"
& icacls.exe $gesperrt /inheritance:r /grant:r "*S-1-5-18:F" | Out-Null
Pruefe ($LASTEXITCODE -eq 0) "c2: nur SYSTEM:F"
$r = Install-Lauf (New-Baum "q1") "2-upgrade-c2"
Pruefe ($r.Rc -eq 0) "Upgrade mit gesperrter Datei: Exit 0 (ist $($r.Rc))"
Pruefe ($r.Text -match "WARNUNG: nicht zurueckgesetzt: gesperrt\.json \(Exit [1-9]\d*: .*Access is denied") "WARNUNG mit echtem Exit-Code und 'Access is denied'"
Pruefe ((Get-DienstStatus) -eq "Running") "Dienst laeuft"
$g = Get-Gesundheit
Pruefe ($null -ne $g -and $g.version -eq $version) "/health meldet $version"

function New-KaputterBaum([string]$Basis, [string]$Name) {
    $b = Join-Path $arbeit $Name
    if (Test-Path $b) { Remove-Item -Recurse -Force $b }
    New-Item -ItemType Directory -Path $b | Out-Null
    foreach ($t in @("app", "windows", "VERSION", "CHANGELOG.md", "LICENSE.md", "NOTICE", "README.md")) {
        $q = Join-Path $Basis $t
        if (Test-Path $q) { Copy-Item -Recurse -Force $q (Join-Path $b $t) }
    }
    Set-Content -Path (Join-Path $b "VERSION") -Value "9.9.9" -NoNewline
    Add-Content -Path (Join-Path $b "app\requirements.lock") -Value "gibt-es-nicht-exo-relay-ci==0.0.1"
    return $b
}

# 3 -- Gegenprobe mit 0.2.45: genau die Labtest-Fehler -----------------------------------
if ($Gegenprobe) {
    $alt = (Get-Content (Join-Path $Gegenprobe "VERSION") -Raw).Trim()
    # 3a: gesperrte Datei (liegt seit Schritt 2 in data\) -> Abbruch an icacls, Dienst gestoppt
    $r = Install-Lauf $Gegenprobe "3a-gegenprobe-0245-c2"
    Pruefe ($r.Rc -ne 0) "Gegenprobe c2: 0.2.45 bricht an der gesperrten Datei ab (Exit $($r.Rc))"
    Pruefe ($r.Text -match "Access is denied") "Gegenprobe c2: Abbruch an 'Access is denied'"
    Pruefe ((Get-DienstStatus) -ne "Running") "Gegenprobe c2: Dienst bleibt gestoppt"
    Pruefe ((Get-Fassung) -eq $alt) "Gegenprobe c2: VERSION schon $alt (halbes Upgrade)"
    $r = Install-Lauf (New-Baum "q1") "3b-wiederherstellen"
    Pruefe ($r.Rc -eq 0 -and (Get-DienstStatus) -eq "Running") "Wiederherstellen nach c2: Exit 0, Dienst laeuft"
    # 3c: an pip scheiterndes Upgrade -> Abbruch ohne Ruecknahme
    $r = Install-Lauf (New-KaputterBaum $Gegenprobe "q0245") "3c-gegenprobe-0245-pip"
    Pruefe ($r.Rc -ne 0) "Gegenprobe pip: 0.2.45 bricht ab (Exit $($r.Rc))"
    Pruefe ((Get-Fassung) -eq "9.9.9") "Gegenprobe pip: VERSION 9.9.9 bleibt liegen (keine Ruecknahme)"
    Pruefe ((Get-DienstStatus) -ne "Running") "Gegenprobe pip: Dienst bleibt gestoppt"
    # 4 -- Wiederherstellen
    $r = Install-Lauf (New-Baum "q1") "4-wiederherstellen"
    Pruefe ($r.Rc -eq 0) "Wiederherstellen: Exit 0 (ist $($r.Rc))"
    Pruefe ((Get-Fassung) -eq $version -and (Get-DienstStatus) -eq "Running") "wieder $version, Dienst laeuft"
}

# 5 -- Upgrade, das an pip scheitert: Ruecknahme ---------------------------------------------
$r = Install-Lauf (New-KaputterBaum $Quelle "q2") "5-abbruch"
Pruefe ($r.Rc -ne 0) "Kaputtes Upgrade: Exit <> 0 (ist $($r.Rc))"
Pruefe ($r.Text -match "ABBRUCH im Schritt 'Abhaengigkeiten'") "Abbruch im Schritt Abhaengigkeiten gemeldet"
Pruefe ($r.Text -match "Vorige Fassung $([regex]::Escape($version)) zurueckgeholt") "vorige Fassung zurueckgeholt"
Pruefe ($r.Text -match "Dienst laeuft wieder \(Fassung $([regex]::Escape($version))\)") "Dienst laeuft wieder gemeldet"
Pruefe ((Get-Fassung) -eq $version) "installierte VERSION wieder $version"
Pruefe ((Get-DienstStatus) -eq "Running") "Dienst laeuft"
Pruefe (-not (Test-Path (Join-Path $ziel ".install-alt"))) ".install-alt\ aufgeraeumt"
$g = Get-Gesundheit
Pruefe ($null -ne $g -and $g.version -eq $version) "/health meldet wieder $version"

# 6 -- e3: windows\ gesperrt --------------------------------------------------------------
function Start-Sperre {
    Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-Command", "Start-Sleep -Seconds 900") `
        -WorkingDirectory (Join-Path $ziel "windows") -WindowStyle Hidden -PassThru
}
function Get-Umgebungsport {
    $u = @((Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\ExoSmtpRelay").Environment)
    foreach ($e in $u) { if ("$e" -match '^WEBUI_PORT=(\d+)$') { return $Matches[1] } }
    return ""
}
$sperre = Start-Sperre
Start-Sleep -Seconds 2
try {
    foreach ($lauf in 1, 2) {
        $r = Install-Lauf (New-Baum "q1") "6-e3-gesperrt-$lauf"
        Pruefe ($r.Rc -ne 0) "e3 Lauf ${lauf}: Exit <> 0 (ist $($r.Rc))"
        Pruefe ($r.Text -match "gesperrt .*windows.* - nichts verschoben") "e3 Lauf ${lauf}: Sperre von windows\ gemeldet"
        Pruefe ($r.Text -match "nichts verschoben, die installierte Fassung ist unveraendert") "e3 Lauf ${lauf}: 'unveraendert' stimmt"
        Pruefe (Test-Path (Join-Path $ziel "app\main.py")) "e3 Lauf ${lauf}: app\ am Platz"
        Pruefe ((Get-Fassung) -eq $version -and (Get-DienstStatus) -eq "Running") "e3 Lauf ${lauf}: $version laeuft"
    }
    $g = Get-Gesundheit
    Pruefe ($null -ne $g -and $g.version -eq $version) "e3: /health meldet $version"
    if ($Gegenprobe046) {
        $r = Install-Lauf $Gegenprobe046 "6g-e3-2ea9192-1"
        Pruefe ($r.Rc -ne 0 -and -not (Test-Path (Join-Path $ziel "app"))) "Gegenprobe e3 2ea9192: app\ fehlt nach dem Abbruch"
        Pruefe ($r.Text -match "unveraendert") "Gegenprobe e3 2ea9192: meldet trotzdem 'unveraendert'"
        $r = Install-Lauf $Gegenprobe046 "6g-e3-2ea9192-2"
        Pruefe (-not (Test-Path (Join-Path $ziel "app")) -and -not (Test-Path (Join-Path $ziel ".install-alt\app"))) "Gegenprobe e3 2ea9192: zweiter Lauf loescht die einzige Sicherung von app\"
    }
} finally {
    Stop-Process -Id $sperre.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
$r = Install-Lauf (New-Baum "q1") "6-e3-entsperrt"
Pruefe ($r.Rc -eq 0 -and (Get-DienstStatus) -eq "Running") "e3: nach Aufheben der Sperre Exit 0, Dienst laeuft"
$g = Get-Gesundheit
Pruefe ($null -ne $g -and $g.version -eq $version) "e3: /health meldet $version"

# 7 -- e4: neue Fassung startet, lauscht aber nicht ------------------------------------------
function New-StummerBaum([string]$Basis, [string]$Name) {
    $b = Join-Path $arbeit $Name
    if (Test-Path $b) { Remove-Item -Recurse -Force $b }
    New-Item -ItemType Directory -Path $b | Out-Null
    foreach ($t in @("app", "windows", "VERSION", "CHANGELOG.md", "LICENSE.md", "NOTICE", "README.md")) {
        $q = Join-Path $Basis $t
        if (Test-Path $q) { Copy-Item -Recurse -Force $q (Join-Path $b $t) }
    }
    Set-Content -Path (Join-Path $b "VERSION") -Value "9.9.8" -NoNewline
    $m = Join-Path $b "app\main.py"
    Set-Content -Path $m -Value (@("import gibt_es_nicht_exo_relay_ci") + @(Get-Content $m)) -Encoding UTF8
    return $b
}
Pruefe ((Get-Umgebungsport) -eq "8443") "e4 vorher: WEBUI_PORT=8443"
$r = Install-Lauf (New-StummerBaum $Quelle "q4") "7-e4-stumm" @("-WebPort", "9443")
Pruefe ($r.Rc -ne 0) "e4: Exit <> 0 (ist $($r.Rc))"
Pruefe ($r.Text -match "ABBRUCH im Schritt 'Lebenszeichen'") "e4: Abbruch im Schritt Lebenszeichen"
Pruefe ($r.Text -match "Vorige Fassung $([regex]::Escape($version)) zurueckgeholt") "e4: vorige Fassung zurueckgeholt"
Pruefe ((Get-Fassung) -eq $version) "e4: VERSION wieder $version"
Pruefe ((Get-Umgebungsport) -eq "8443") "e4: WEBUI_PORT wieder 8443"
$fw = @(Get-NetFirewallRule -DisplayName "EXO SMTP Relay - Web *")
Pruefe ($fw.Count -eq 1 -and "$((Get-NetFirewallPortFilter -AssociatedNetFirewallRule $fw[0]).LocalPort)" -eq "8443") "e4: Firewall wieder Port 8443"
$g = Get-Gesundheit
Pruefe ($null -ne $g -and $g.version -eq $version) "e4: /health auf 8443 meldet $version"
if ($Gegenprobe046) {
    $r = Install-Lauf (New-StummerBaum $Gegenprobe046 "q4g") "7g-e4-2ea9192"
    Pruefe ($r.Rc -eq 0 -and $r.Text -match "Aktualisiert") "Gegenprobe e4 2ea9192: meldet 'Aktualisiert' (Exit $($r.Rc))"
    $roh = & curl.exe -sk --max-time 5 "https://localhost:8443/health" 2>$null
    Pruefe (-not $roh) "Gegenprobe e4 2ea9192: /health bleibt stumm"
    $r = Install-Lauf (New-Baum "q1") "7g-wiederherstellen"
    Pruefe ($r.Rc -eq 0 -and (Get-Fassung) -eq $version) "Wiederherstellen nach e4-Gegenprobe"
}

Write-Host "`nAlle Pruefungen bestanden." -ForegroundColor Green
