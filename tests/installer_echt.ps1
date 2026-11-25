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

      8. e6a: vorige Fassung mit Lock OHNE Pruefsummen, neue mit idna 3.10 und kaputter main.py ->
         Ruecknahme setzt idna auf 3.18 zurueck; Gegenprobe efbf608: idna bleibt 3.10
      9. e5: windows\ erst WAEHREND der Lebenszeichen-Wartezeit gesperrt, -WebPort 9443 ->
         UNVOLLSTAENDIG, Dienst-Umgebung UND Firewall 8443 (app\ kam zurueck); Folgelauf bei weiter
         bestehender Sperre loescht die Sicherung NICHT; nach dem Entsperren kommt die alte
         windows\ zurueck. Gegenprobe efbf608: Firewall 8443, WEBUI_PORT 9443; Folgelauf loescht
         die Sicherung ("alle Teile am Platz")

     10. Z1: Sperre IN .install-alt\windows waehrend des Lebenszeichens, kaputte Fassung -> windows\
         bleibt am Platz (die neue), Dienst laeuft mit der alten app\; Folgelauf unter Sperre ebenso.
         Gegenprobe 97cc8bf: kein windows\ am Platz, Dienst gestoppt
     11. Z2: dieselbe Sperre bei einem ERFOLGREICHEN Upgrade -> Exit 0 mit WARNUNG zum Rest; Folgelauf
         unter Sperre bricht ab, ohne das gute windows\ anzufassen. Gegenprobe 97cc8bf: Folgelauf
         verwirft das gute windows\, Dienst gestoppt

     12. OFFENE DATEI (FileShare Read, kein Delete) in app\main.py, windows\service.py und - waehrend
         des Lebenszeichens - in .install-alt\windows\service.py: nichts teilweise verschoben,
         ehrliche Meldung, Dienst laeuft weiter bzw. wieder; Folgelauf unter Sperre ebenso,
         entsperrt Exit 0. Gegenprobe aa45770: Teilverschiebung sichtbar (*.relay-sperrtest bzw.
         unvollstaendiges windows\); danach heilt der aktuelle Installer das ohne Handarbeit.

    -Gegenprobe050 gibt den Pfad eines aa45770-Baums an (0.2.50, vom Labtest gestoppt).
    -Gegenprobe049 gibt den Pfad eines 97cc8bf-Baums an (0.2.49-beta).
    -Gegenprobe048 gibt den Pfad eines efbf608-Baums an (0.2.48-beta).
    -Gegenprobe046 gibt den Pfad eines 2ea9192-Baums an (0.2.46, vom Labtest gestoppt).
    -Gegenprobe gibt den Pfad eines entpackten 0.2.45-Baums an (CI: git worktree).
#>
param(
    [string]$Quelle = (Split-Path -Parent $PSScriptRoot),
    [string]$Gegenprobe = "",
    [string]$Gegenprobe046 = "",
    [string]$Gegenprobe048 = "",
    [string]$Gegenprobe049 = "",
    [string]$Gegenprobe050 = ""
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

# 8 -- e6a: Ruecknahme auf eine Fassung mit Lock ohne Pruefsummen --------------------------
function Get-Idna { "$((& "$ziel\venv\Scripts\python.exe" -m pip show idna 2>$null | Select-String '^Version:') -replace 'Version:\s*','')".Trim() }
function Set-IdnaAlt([string]$Baum) {
    $lock = Join-Path $Baum "app\requirements.lock"
    $text = Get-Content -LiteralPath $lock -Raw
    $neu = [regex]::Replace($text, '(?m)^idna==3\.18 \\\r?\n\s+--hash=sha256:[0-9a-f]{64}',
        "idna==3.10 \`n    --hash=sha256:946d195a0d259cbba61165e88e65941f16e9b36ea6ddb97f00452bae8b1287d3")
    if ($neu -eq $text) { throw "idna-Eintrag nicht gefunden in $lock" }
    Set-Content -LiteralPath $lock -Value $neu -NoNewline -Encoding UTF8
}
$qa = New-Baum "q6a"
Set-Content -Path (Join-Path $qa "VERSION") -Value "9.9.1" -NoNewline
$la = Join-Path $qa "app\requirements.lock"
Set-Content -LiteralPath $la -Encoding UTF8 -Value @(Get-Content -LiteralPath $la | Where-Object { $_ -notmatch '^\s+--hash=' } | ForEach-Object { $_ -replace '\s*\\$', '' })
Pruefe (-not (Select-String -LiteralPath $la -Pattern "--hash=" -SimpleMatch -Quiet)) "e6a: vorige Fassung hat eine Lock ohne Pruefsummen"
$r = Install-Lauf $qa "8-e6a-vorher"
Pruefe ($r.Rc -eq 0 -and (Get-Fassung) -eq "9.9.1") "e6a: Fassung 9.9.1 (Lock ohne Pruefsummen) installiert"
Pruefe ((Get-Idna) -eq "3.18") "e6a: idna 3.18"
$qb = New-StummerBaum $Quelle "q6b"
Set-IdnaAlt $qb
$r = Install-Lauf $qb "8-e6a-kaputt"
Pruefe ($r.Rc -ne 0 -and $r.Text -match "ABBRUCH im Schritt 'Lebenszeichen'") "e6a: neue Fassung (idna 3.10) faellt am Lebenszeichen"
Pruefe ($r.Text -match "Abhaengigkeiten auf Fassung 9\.9\.1 zurueckgesetzt") "e6a: Abhaengigkeiten zurueckgesetzt gemeldet"
Pruefe ((Get-Idna) -eq "3.18") "e6a: idna nach der Ruecknahme wieder 3.18 (ist $(Get-Idna))"
Pruefe ((Get-Fassung) -eq "9.9.1") "e6a: VERSION wieder 9.9.1"
if ($Gegenprobe048) {
    $qg = New-StummerBaum $Gegenprobe048 "q6g"
    Set-IdnaAlt $qg
    $r = Install-Lauf $qg "8g-e6a-efbf608"
    Pruefe ($r.Rc -ne 0 -and $r.Text -match "Abhaengigkeiten nicht zurueckgesetzt") "Gegenprobe e6a efbf608: Ruecknahme der Abhaengigkeiten scheitert"
    Pruefe ((Get-Idna) -eq "3.10") "Gegenprobe e6a efbf608: idna bleibt 3.10"
}
$r = Install-Lauf (New-Baum "q1") "8-e6a-wiederherstellen"
Pruefe ($r.Rc -eq 0 -and (Get-Fassung) -eq $version -and (Get-Idna) -eq "3.18") "e6a: wiederhergestellt ($version, idna 3.18)"

# 9 -- e5: Sperre erst waehrend der Lebenszeichen-Wartezeit --------------------------------
function Start-SpaeteSperre([string]$Fassung) {
    $pidDatei = Join-Path $arbeit "sperre-$([guid]::NewGuid().ToString('N')).pid"
    $code = "while (-not ((Get-Service ExoSmtpRelay).Status -eq 'Running' -and (Test-Path '$ziel\VERSION') -and (Get-Content '$ziel\VERSION' -Raw).Trim() -eq '$Fassung')) { Start-Sleep -Milliseconds 300 }; " +
            "`$p = Start-Process powershell.exe -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 900' -WorkingDirectory '$ziel\windows' -WindowStyle Hidden -PassThru; Set-Content '$pidDatei' `$p.Id"
    $w = Start-Process powershell.exe -ArgumentList @("-NoProfile", "-Command", $code) -WindowStyle Hidden -PassThru
    return @{ Waechter = $w; PidDatei = $pidDatei }
}
function Stop-Sperre($s) {
    Stop-Process -Id $s.Waechter.Id -Force -ErrorAction SilentlyContinue
    if (Test-Path $s.PidDatei) { Stop-Process -Id ([int](Get-Content $s.PidDatei)) -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}
function Get-FirewallPort { $fw = @(Get-NetFirewallRule -DisplayName "EXO SMTP Relay - Web *"); "$((Get-NetFirewallPortFilter -AssociatedNetFirewallRule $fw[0]).LocalPort)" }
$sich = Join-Path $ziel ".install-alt"

$s = Start-SpaeteSperre "9.9.8"
try {
    $r = Install-Lauf (New-StummerBaum $Quelle "q5") "9-e5-1" @("-WebPort", "9443")
    Pruefe (Test-Path $s.PidDatei) "e5: Sperre griff erst waehrend der Lebenszeichen-Wartezeit"
    Pruefe ($r.Rc -ne 0 -and $r.Text -match "Ruecknahme UNVOLLSTAENDIG: windows") "e5: Ruecknahme UNVOLLSTAENDIG (windows) gemeldet"
    Pruefe ((Get-Fassung) -eq $version) "e5: VERSION wieder $version (app\ zurueck)"
    Pruefe ((Get-Umgebungsport) -eq "8443" -and (Get-FirewallPort) -eq "8443") "e5a: Dienst-Umgebung UND Firewall wieder 8443 (ist $(Get-Umgebungsport)/$(Get-FirewallPort))"
    Pruefe ((Test-Path "$sich\windows") -and (Test-Path "$sich\zurueckholen.json")) "e5: alte windows\ mit Merker in .install-alt"
    $r = Install-Lauf (New-Baum "q1") "9-e5-2"
    Pruefe ($r.Rc -ne 0 -and $r.Text -match "nicht zurueckholbar: windows") "e5b: Folgelauf bei Sperre bricht ab, ohne den Rest anzufassen"
    Pruefe ($r.Text -notmatch "Vorige Fassung .* zurueckgeholt" -and $r.Text -notmatch "alle Teile am Platz") "e5b: keine falsche Erfolgsmeldung, kein Loeschen"
    Pruefe (Test-Path "$sich\windows") "e5b: Sicherung der alten windows\ bleibt"
} finally { Stop-Sperre $s }
$r = Install-Lauf (New-Baum "q1") "9-e5-3"
Pruefe ($r.Rc -eq 0 -and $r.Text -match "zurueckgeholt \(Fassung $([regex]::Escape($version))\): windows") "e5: nach dem Entsperren alte windows\ zurueck, Exit 0"
$g = Get-Gesundheit
Pruefe ($null -ne $g -and $g.version -eq $version -and -not (Test-Path $sich)) "e5: /health $version, .install-alt weg"

if ($Gegenprobe048) {
    $s = Start-SpaeteSperre "9.9.8"
    try {
        $r = Install-Lauf (New-StummerBaum $Gegenprobe048 "q5g") "9g-e5-efbf608-1" @("-WebPort", "9443")
        Pruefe ((Get-FirewallPort) -eq "8443" -and (Get-Umgebungsport) -eq "9443") "Gegenprobe e5a efbf608: Firewall 8443, WEBUI_PORT 9443 (uneinheitlich)"
        $r = Install-Lauf $Gegenprobe048 "9g-e5-efbf608-2"
        Pruefe ($r.Text -match "alle Teile am Platz" -and -not (Test-Path "$sich\windows")) "Gegenprobe e5b efbf608: Folgelauf loescht die Sicherung der alten windows\"
    } finally { Stop-Sperre $s }
    $r = Install-Lauf (New-Baum "q1") "9g-wiederherstellen" @("-WebPort", "8443")
    Pruefe ($r.Rc -eq 0 -and (Get-Fassung) -eq $version -and (Get-Umgebungsport) -eq "8443") "Wiederherstellen nach e5-Gegenprobe"
}

# 10/11 -- Sperre IN .install-alt\windows --------------------------------------------------
function Start-SicherungsSperre([string]$Fassung) {
    $pidDatei = Join-Path $arbeit "sperre-$([guid]::NewGuid().ToString('N')).pid"
    $code = "while (-not ((Get-Service ExoSmtpRelay).Status -eq 'Running' -and (Test-Path '$ziel\.install-alt\windows') -and (Test-Path '$ziel\VERSION') -and (Get-Content '$ziel\VERSION' -Raw).Trim() -eq '$Fassung')) { Start-Sleep -Milliseconds 200 }; " +
            "`$p = Start-Process powershell.exe -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 900' -WorkingDirectory '$ziel\.install-alt\windows' -WindowStyle Hidden -PassThru; Set-Content '$pidDatei' `$p.Id"
    $w = Start-Process powershell.exe -ArgumentList @("-NoProfile", "-Command", $code) -WindowStyle Hidden -PassThru
    return @{ Waechter = $w; PidDatei = $pidDatei }
}
function New-Baum997([string]$Basis, [string]$Name) {
    $b = Join-Path $arbeit $Name
    if (Test-Path $b) { Remove-Item -Recurse -Force $b }
    New-Item -ItemType Directory -Path $b | Out-Null
    foreach ($t in @("app", "windows", "VERSION", "CHANGELOG.md", "LICENSE.md", "NOTICE", "README.md")) {
        $q = Join-Path $Basis $t
        if (Test-Path $q) { Copy-Item -Recurse -Force $q (Join-Path $b $t) }
    }
    Set-Content -Path (Join-Path $b "VERSION") -Value "9.9.7" -NoNewline
    return $b
}
function Test-Laeuft([string]$Soll) { $g = Get-Gesundheit; return ((Get-DienstStatus) -eq "Running" -and $null -ne $g -and $g.version -eq $Soll) }

# Z1
$s = Start-SicherungsSperre "9.9.8"
try {
    $r = Install-Lauf (New-StummerBaum $Quelle "qz1") "10-z1-1"
    Pruefe (Test-Path $s.PidDatei) "Z1: Sperre in .install-alt\windows griff waehrend des Lebenszeichens"
    Pruefe ($r.Rc -ne 0 -and $r.Text -match "Ruecknahme UNVOLLSTAENDIG: windows") "Z1: UNVOLLSTAENDIG (windows) gemeldet"
    Pruefe (Test-Path (Join-Path $ziel "windows\service.py")) "Z1: windows\ am Platz (die neue - lieber als keine)"
    Pruefe (Test-Laeuft $version) "Z1: Dienst laeuft, /health $version"
    $r = Install-Lauf (New-Baum "q1") "10-z1-2"
    Pruefe ($r.Rc -ne 0 -and $r.Text -match "nicht zurueckholbar: windows") "Z1 Folgelauf: bricht ab"
    Pruefe ((Test-Path (Join-Path $ziel "windows\service.py")) -and (Test-Laeuft $version)) "Z1 Folgelauf: windows\ am Platz, Dienst laeuft mit $version"
} finally { Stop-Sperre $s }
$r = Install-Lauf (New-Baum "q1") "10-z1-3"
Pruefe ($r.Rc -eq 0 -and (Test-Laeuft $version) -and -not (Test-Path (Join-Path $ziel ".install-alt"))) "Z1: nach dem Entsperren Exit 0, $version, kein Rest"
if ($Gegenprobe049) {
    $s = Start-SicherungsSperre "9.9.8"
    try {
        $r = Install-Lauf (New-StummerBaum $Gegenprobe049 "qz1g") "10g-z1-97cc8bf"
        Pruefe (-not (Test-Path (Join-Path $ziel "windows")) -and (Get-DienstStatus) -ne "Running") "Gegenprobe Z1 97cc8bf: kein windows\ am Platz, Dienst gestoppt"
    } finally { Stop-Sperre $s }
    $r = Install-Lauf (New-Baum "q1") "10g-wiederherstellen"
    Pruefe ($r.Rc -eq 0 -and (Test-Laeuft $version)) "Wiederherstellen nach Z1-Gegenprobe"
}

# Z2
$s = Start-SicherungsSperre "9.9.7"
try {
    $r = Install-Lauf (New-Baum997 $Quelle "qz2") "11-z2-1"
    Pruefe (Test-Path $s.PidDatei) "Z2: Sperre in .install-alt\windows griff waehrend des Lebenszeichens"
    Pruefe ($r.Rc -eq 0 -and $r.Text -match "nicht loeschbar \(gesperrt\?\) - kein Fehler") "Z2: Upgrade erfolgreich, Exit 0, WARNUNG zum Rest"
    Pruefe (Test-Laeuft "9.9.7") "Z2: 9.9.7 laeuft"
    $r = Install-Lauf (New-Baum "q1") "11-z2-2"
    Pruefe ($r.Rc -ne 0 -and $r.Text -match "nicht entfernbar") "Z2 Folgelauf: bricht am Rest ab"
    Pruefe ($r.Text -notmatch "zurueckgeholt") "Z2 Folgelauf: nichts 'zurueckgeholt'"
    Pruefe ((Test-Path (Join-Path $ziel "windows\service.py")) -and (Test-Laeuft "9.9.7")) "Z2 Folgelauf: gutes windows\ bleibt, 9.9.7 laeuft"
} finally { Stop-Sperre $s }
$r = Install-Lauf (New-Baum "q1") "11-z2-3"
Pruefe ($r.Rc -eq 0 -and (Test-Laeuft $version) -and -not (Test-Path (Join-Path $ziel ".install-alt"))) "Z2: nach dem Entsperren Exit 0, $version, kein Rest"
if ($Gegenprobe049) {
    $s = Start-SicherungsSperre "9.9.7"
    try {
        $r = Install-Lauf (New-Baum997 $Gegenprobe049 "qz2g") "11g-z2-97cc8bf-1"
        Pruefe ($r.Rc -eq 0) "Gegenprobe Z2 97cc8bf: Upgrade Exit 0 (Rest bleibt still)"
        $r = Install-Lauf $Gegenprobe049 "11g-z2-97cc8bf-2"
        Pruefe ((-not (Test-Path (Join-Path $ziel "windows\service.py"))) -or (Get-DienstStatus) -ne "Running") "Gegenprobe Z2 97cc8bf: Folgelauf verwirft das gute windows\ bzw. Dienst gestoppt"
    } finally { Stop-Sperre $s }
    $r = Install-Lauf (New-Baum "q1") "11g-wiederherstellen"
    Pruefe ($r.Rc -eq 0 -and (Test-Laeuft $version)) "Wiederherstellen nach Z2-Gegenprobe"
}

# 12 -- offene Dateien -----------------------------------------------------------------
function Start-DateiSperre([string]$Datei, [string]$Fassung = "") {
    $pidDatei = Join-Path $arbeit "dsperre-$([guid]::NewGuid().ToString('N')).pid"
    $warte = ""
    if ($Fassung) {
        $warte = "while (-not ((Get-Service ExoSmtpRelay).Status -eq 'Running' -and (Test-Path '$Datei') -and (Get-Content '$ziel\VERSION' -Raw).Trim() -eq '$Fassung')) { Start-Sleep -Milliseconds 200 }; "
    }
    $code = $warte + "`$h = [System.IO.File]::Open('$Datei', 'Open', 'Read', 'Read'); Set-Content '$pidDatei' `$PID; Start-Sleep -Seconds 900"
    $w = Start-Process powershell.exe -ArgumentList @("-NoProfile", "-Command", $code) -WindowStyle Hidden -PassThru
    if (-not $Fassung) { for ($i = 0; $i -lt 50 -and -not (Test-Path $pidDatei); $i++) { Start-Sleep -Milliseconds 200 } }
    return @{ Waechter = $w; PidDatei = $pidDatei }
}
function Get-Anzahl([string]$Ordner) {
    if (-not (Test-Path $Ordner)) { return 0 }
    @(Get-ChildItem -LiteralPath $Ordner -Recurse -File -Force | Where-Object { $_.FullName -notmatch '\\__pycache__\\' }).Count
}
function Get-Reste { @(Get-ChildItem -LiteralPath $ziel -Force | Where-Object { $_.Name -match '\.relay-(sperrtest|verworfen)' } | ForEach-Object { $_.Name }) }

foreach ($fall in @(@{ Teil = "app"; Datei = "app\main.py" }, @{ Teil = "windows"; Datei = "windows\service.py" })) {
    $teil = $fall.Teil
    $vorher = Get-Anzahl (Join-Path $ziel $teil)
    $s = Start-DateiSperre (Join-Path $ziel $fall.Datei)
    try {
        Pruefe (Test-Path $s.PidDatei) "offen ${teil}: Datei $($fall.Datei) offen (Share Read)"
        foreach ($lauf in 1, 2) {
            $r = Install-Lauf (New-Baum "q1") "12-$teil-$lauf"
            Pruefe ($r.Rc -ne 0 -and $r.Text -match "gesperrt .*$teil.* - nichts verschoben") "offen $teil Lauf ${lauf}: Sperre gemeldet, nichts verschoben"
            Pruefe ((Get-Anzahl (Join-Path $ziel $teil)) -eq $vorher -and @(Get-Reste).Count -eq 0) "offen $teil Lauf ${lauf}: $vorher Dateien am Platz, keine Reste"
            Pruefe (Test-Laeuft $version) "offen $teil Lauf ${lauf}: Dienst laeuft, /health $version"
        }
        if ($Gegenprobe050) {
            $r = Install-Lauf $Gegenprobe050 "12g-$teil-aa45770"
            Pruefe ((Get-Reste) -contains "$teil.relay-sperrtest") "Gegenprobe offen $teil aa45770: Teilverschiebung sichtbar ($((Get-Reste) -join ', '); $(Get-Anzahl (Join-Path $ziel $teil)) von $vorher am Platz)"
            $r = Install-Lauf (New-Baum "q1") "12h-$teil-heilen"
            Pruefe ($r.Text -match "\.relay-sperrtest: \d+ Datei\(en\) nach $teil\\ zurueckgefuehrt") "Heilung offen ${teil}: Reste zurueckgefuehrt (unter Sperre)"
            Pruefe ((Get-Anzahl (Join-Path $ziel $teil)) -eq $vorher -and @(Get-Reste).Count -eq 0) "Heilung offen ${teil}: wieder $vorher Dateien, keine Reste"
        }
    } finally { Stop-Sperre $s }
    $r = Install-Lauf (New-Baum "q1") "12-$teil-entsperrt"
    Pruefe ($r.Rc -eq 0 -and (Test-Laeuft $version) -and @(Get-Reste).Count -eq 0) "offen ${teil}: entsperrt Exit 0, $version, keine Reste"
}

# offene Datei in .install-alt\windows waehrend des Lebenszeichens (kaputte neue Fassung)
$sollWindows = Get-Anzahl (Join-Path $ziel "windows")
$s = Start-DateiSperre (Join-Path $ziel ".install-alt\windows\service.py") "9.9.8"
try {
    $r = Install-Lauf (New-StummerBaum $Quelle "qo1") "12-alt-1"
    Pruefe (Test-Path $s.PidDatei) "offen .install-alt: Datei waehrend des Lebenszeichens geoeffnet"
    Pruefe ($r.Rc -ne 0 -and $r.Text -match "Ruecknahme UNVOLLSTAENDIG: windows") "offen .install-alt: UNVOLLSTAENDIG (windows) gemeldet"
    Pruefe ((Get-Anzahl (Join-Path $ziel "windows")) -eq $sollWindows -and (Get-Anzahl (Join-Path $ziel ".install-alt\windows")) -eq $sollWindows) "offen .install-alt: beide windows\ vollstaendig ($sollWindows Dateien), nichts geteilt"
    Pruefe (Test-Laeuft $version) "offen .install-alt: Dienst laeuft, /health $version"
    $r = Install-Lauf (New-Baum "q1") "12-alt-2"
    Pruefe ($r.Rc -ne 0 -and $r.Text -match "nicht zurueckholbar: windows" -and $r.Text -notmatch "Vorige Fassung .* zurueckgeholt") "offen .install-alt Folgelauf: bricht ehrlich ab"
    Pruefe ((Get-Anzahl (Join-Path $ziel "windows")) -eq $sollWindows -and (Test-Laeuft $version)) "offen .install-alt Folgelauf: windows\ vollstaendig, Dienst laeuft"
} finally { Stop-Sperre $s }
$r = Install-Lauf (New-Baum "q1") "12-alt-3"
Pruefe ($r.Rc -eq 0 -and (Test-Laeuft $version) -and @(Get-Reste).Count -eq 0 -and -not (Test-Path (Join-Path $ziel ".install-alt"))) "offen .install-alt: entsperrt Exit 0, keine Reste"
if ($Gegenprobe050) {
    $s = Start-DateiSperre (Join-Path $ziel ".install-alt\windows\service.py") "9.9.8"
    try {
        $r = Install-Lauf (New-StummerBaum $Gegenprobe050 "qo1g") "12g-alt-aa45770"
        Pruefe ((Get-Anzahl (Join-Path $ziel "windows")) -lt $sollWindows) "Gegenprobe offen .install-alt aa45770: windows\ am Platz unvollstaendig ($(Get-Anzahl (Join-Path $ziel 'windows')) von $sollWindows)"
    } finally { Stop-Sperre $s }
    $r = Install-Lauf (New-Baum "q1") "12h-alt-heilen"
    Pruefe ($r.Rc -eq 0 -and (Test-Laeuft $version) -and (Get-Anzahl (Join-Path $ziel "windows")) -eq $sollWindows -and @(Get-Reste).Count -eq 0) "Heilung nach aa45770: Exit 0, windows\ vollstaendig, keine Reste"
}

Write-Host "`nAlle Pruefungen bestanden." -ForegroundColor Green
# Der Actions-Wrapper beendet mit $LASTEXITCODE - das letzte curl der Gegenprobe ist absichtlich gescheitert.
exit 0
