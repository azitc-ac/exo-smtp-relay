<#
.SYNOPSIS
    Offline-Paket auf einem Windows-Rechner OHNE Netz fuer pip installieren und pruefen
    (CI-Jobs "Offline-Paket" in ci.yml und release.yml). Installiert den Dienst wirklich.

.DESCRIPTION
      1. Paket entpacken; Python-Installer: SHA256 und Authenticode (Python Software Foundation)
         ECHT pruefen - Gegenproben: ein veraendertes Byte und ein falscher Signierer muessen
         abgelehnt werden.
      2. ExchangeOnlineManagement aus dem nupkg: Pruefsumme, Microsoft-Signatur jeder
         Skriptdatei, Import-Module 3.10.1.
      3. Netz fuer pip und Python sperren: PIP_NO_INDEX, toter Index und Proxy, ausgehende
         Firewall-Regel fuer alle python.exe. Probe: python erreicht pypi.org NICHT.
      4. install.ps1 -Unattended -> Exit 0, "Offline-Installation", pip --no-index, Dienst laeuft,
         /health meldet VERSION, offline\wheels liegt in der Installation.
      5. Updater-Weg: update_tausch.phase_pip installiert offline aus offline\wheels.
#>
param([Parameter(Mandatory = $true)][string]$Paket)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$basis = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$arbeit = Join-Path $basis "offline-probe"
$ziel = "C:\ProgramData\exo-smtp-relay"

function Pruefe([bool]$Bedingung, [string]$Text) {
    if (-not $Bedingung) { throw "FEHLGESCHLAGEN: $Text" }
    Write-Host "  OK  $Text" -ForegroundColor Green
}

Write-Host "Paket: $Paket ($([math]::Round((Get-Item $Paket).Length / 1MB, 1)) MB)"
if (Test-Path $arbeit) { Remove-Item -Recurse -Force $arbeit }
Expand-Archive -LiteralPath $Paket -DestinationPath $arbeit
$baum = @(Get-ChildItem -LiteralPath $arbeit -Directory)[0].FullName
$install = Join-Path $baum "windows\install.ps1"
Pruefe (Test-Path (Join-Path $baum "offline\inhalt.json")) "offline\inhalt.json im Paket"

# -- 1/2: Pruefungen mit den ECHTEN Funktionen aus install.ps1 ----------------------
$text = Get-Content -LiteralPath $install -Raw -Encoding UTF8
$teile = @()
foreach ($n in @("RelayPythonInstaller", "RelayExoModul")) {
    $m = [regex]::Match($text, "(?ms)^\`$$n = @\{.*?^\}")
    Pruefe $m.Success "Tabelle `$$n in install.ps1"
    $teile += $m.Value
}
foreach ($n in @("Test-RelaySignatur", "Test-RelayDatei", "Move-RelayPfad", "Get-RelayFreierName", "Install-RelayExoModul")) {
    $m = [regex]::Match($text, "(?ms)^function $n\b.*?^\}")
    Pruefe $m.Success "Funktion $n in install.ps1"
    $teile += $m.Value
}
. ([scriptblock]::Create($teile -join "`n"))

$pyDatei = Join-Path $baum "offline\python\$($RelayPythonInstaller.Datei)"
Test-RelayDatei $pyDatei $RelayPythonInstaller
$sig = Get-AuthenticodeSignature -FilePath $pyDatei
Pruefe ("$($sig.Status)" -eq "Valid") "Python-Installer: Authenticode $($sig.Status), $($sig.SignerCertificate.Subject)"

$kaputt = Join-Path $arbeit "kaputt.exe"
Copy-Item $pyDatei $kaputt
$fs = [IO.File]::Open($kaputt, "Append"); $fs.WriteByte(0); $fs.Close()
$abgelehnt = $false
try { Test-RelayDatei $kaputt $RelayPythonInstaller } catch { $abgelehnt = "$($_.Exception.Message)" -match "Pruefsumme stimmt nicht" }
Pruefe $abgelehnt "Gegenprobe: veraenderter Installer abgelehnt (Pruefsumme)"
$abgelehnt = $false
try { Test-RelaySignatur $pyDatei "Jemand Anderes" } catch { $abgelehnt = "$($_.Exception.Message)" -match "erwartet 'Jemand Anderes'" }
Pruefe $abgelehnt "Gegenprobe: falscher Signierer abgelehnt (Authenticode)"
$unsigniert = Join-Path $arbeit "unsigniert.ps1"
Set-Content -Path $unsigniert -Value "'x'"
$abgelehnt = $false
try { Test-RelaySignatur $unsigniert "Microsoft Corporation" } catch { $abgelehnt = "$($_.Exception.Message)" -match "nicht gueltig" }
Pruefe $abgelehnt "Gegenprobe: unsignierte Datei abgelehnt"

$wohin = Install-RelayExoModul (Join-Path $baum "offline\module\$($RelayExoModul.Datei)")
Pruefe (Test-Path (Join-Path $wohin "ExchangeOnlineManagement.psd1")) "ExchangeOnlineManagement $($RelayExoModul.Version) installiert: $wohin"
$alt = $env:PSModulePath; $env:PSModulePath = ""
$imp = & powershell.exe -NoProfile -Command "Import-Module ExchangeOnlineManagement -RequiredVersion $($RelayExoModul.Version) -ErrorAction Stop; (Get-Module ExchangeOnlineManagement).Version.ToString()"
$env:PSModulePath = $alt
Pruefe ("$imp".Trim() -eq $RelayExoModul.Version) "Import-Module ExchangeOnlineManagement $($RelayExoModul.Version) (PS 5.1)"

# -- 3: Netz fuer pip/Python sperren -------------------------------------------------
$env:PIP_NO_INDEX = "1"
$env:PIP_INDEX_URL = "http://127.0.0.1:9/simple"
$env:PIP_EXTRA_INDEX_URL = ""
$env:HTTP_PROXY = "http://127.0.0.1:9"
$env:HTTPS_PROXY = "http://127.0.0.1:9"
$env:NO_PROXY = "localhost,127.0.0.1"
$pythons = @(Get-ChildItem "C:\hostedtoolcache\windows\Python" -Recurse -Filter python.exe -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
$pythons += @("$ziel\venv\Scripts\python.exe")
$i = 0
foreach ($p in $pythons) {
    $i++
    New-NetFirewallRule -DisplayName "Offline-Probe: kein Netz fuer Python $i" -Direction Outbound -Action Block -Program $p | Out-Null
}
Write-Host "  Firewall-Profile: $((Get-NetFirewallProfile | ForEach-Object { "$($_.Name)=$($_.Enabled)" }) -join ', '); $($pythons.Count) Python-Programme gesperrt"
$py = (Get-Command python).Source
$netz = & $py -c "import urllib.request
try:
    urllib.request.urlopen('https://pypi.org/simple/', timeout=10); print('ERREICHBAR')
except Exception as e:
    print('GESPERRT', type(e).__name__)" 2>&1
Pruefe ("$netz" -match "^GESPERRT") "Python erreicht pypi.org nicht ($netz)"

# -- 4: Installation -----------------------------------------------------------------
$log = Join-Path $arbeit "install.log"
$env:PSModulePath = ""
$p = Start-Process -FilePath "powershell.exe" -Wait -PassThru -NoNewWindow -RedirectStandardOutput $log -RedirectStandardError "$log.err" `
    -ArgumentList @("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $install, "-Unattended")
$env:PSModulePath = $alt
$ausgabe = (Get-Content $log -Raw) + "`n" + (Get-Content "$log.err" -Raw -ErrorAction SilentlyContinue)
Write-Host $ausgabe
Pruefe ($p.ExitCode -eq 0) "install.ps1 offline: Exit 0 (ist $($p.ExitCode))"
Pruefe ($ausgabe -match "Offline-Installation") "meldet 'Offline-Installation'"
Pruefe ($ausgabe -match "Offline: pip --no-index aus offline\\wheels") "pip ohne Index aus offline\wheels"
Pruefe ("$((Get-Service ExoSmtpRelay).Status)" -eq "Running") "Dienst laeuft"
$version = (Get-Content (Join-Path $baum "VERSION") -Raw).Trim()
$g = $null
for ($n = 0; $n -lt 40 -and -not $g; $n++) {
    $roh = & curl.exe -sk --max-time 5 "https://localhost:8443/health" 2>$null
    if ($LASTEXITCODE -eq 0 -and $roh) { try { $g = $roh | ConvertFrom-Json } catch { } }
    if (-not $g) { Start-Sleep -Seconds 3 }
}
Pruefe ($null -ne $g -and $g.version -eq $version) "/health meldet $version"
Pruefe (Test-Path "$ziel\offline\wheels") "offline\wheels in der Installation"
Pruefe (-not (Test-Path "$ziel\offline\python")) "kein Python-Installer in der Installation"
$liste = & "$ziel\venv\Scripts\python.exe" -m pip list --format freeze 2>$null
Pruefe ("$liste" -match "cryptography==50\.0\.0") "cryptography 50.0.0 in der venv"

# -- 5: Updater-Weg ohne Netz ------------------------------------------------------
$code = "import sys, pathlib; sys.path.insert(0, r'$ziel\app'); import update_tausch; print(update_tausch.phase_pip(pathlib.Path(r'$ziel')))"
$upd = & "$ziel\venv\Scripts\python.exe" -c $code 2>&1
Pruefe ("$upd" -match "offline, mitgelieferte Wheels") "Updater: phase_pip offline ($upd)"

Write-Host "`nOffline-Paket: alle Pruefungen bestanden." -ForegroundColor Green
