#Requires -Modules ExchangeOnlineManagement
<#
.SYNOPSIS
    Legt den Inbound-Connector fuer das EXO SMTP Relay an (oder zieht ihn nach).

.DESCRIPTION
    Exchange Online nimmt Post vom Relay nur ueber einen Inbound-Connector vom
    Typ OnPremises an. Der Connector erkennt das Relay entweder am
    TLS-Zertifikat (-RelayHostname, braucht ein Zertifikat einer oeffentlichen
    CA) oder an der Quelladresse (-SenderIPAddresses, braucht eine feste
    oeffentliche IP). Wird -SenderIPAddresses angegeben, gilt die Adressvariante.

    Idempotent: Ein vorhandener Connector gleichen Namens wird aktualisiert.
    Kein Outbound-Connector, keine Transportregel — das Relay leitet nur ein.

    Laeuft unter Windows PowerShell 5.1 und PowerShell 7.

.PARAMETER AppId
    Application (client) ID der App-Registrierung (Exchange.ManageAsApp).

.PARAMETER Organization
    Erstdomaene des Tenants, z. B. "contoso.onmicrosoft.com".

.PARAMETER CertPath
    Pfad zur PFX (ohne Passwort) mit dem Auth-Zertifikat der App.

.PARAMETER RelayHostname
    Hostname des Relays, wie er im TLS-Zertifikat steht (CN/SAN).

.PARAMETER SenderIPAddresses
    Oeffentliche IP-Adressen des Relays, durch Komma getrennt. Wenn gesetzt,
    erkennt der Connector das Relay an der Adresse statt am Zertifikat.

.PARAMETER ConnectorName
    Name des Connectors. Vorgabe: "EXO SMTP Relay - Inbound".
#>
param(
    [Parameter(Mandatory = $true)][string]$AppId,
    [Parameter(Mandatory = $true)][string]$Organization,
    [Parameter(Mandatory = $true)][string]$CertPath,
    [Parameter(Mandatory = $true)][string]$RelayHostname,
    [string]$SenderIPAddresses = "",
    [string]$ConnectorName = "EXO SMTP Relay - Inbound"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step([string]$msg) { Write-Host "[RELAY-SETUP] $msg" -ForegroundColor Cyan }
function Write-OK([string]$msg)   { Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }

$managedBy = "##Managed by EXO SMTP Relay, last update: $(Get-Date -Format 'yyyy-MM-dd HH:mm')##"

# -- Zertifikat laden (PFX ohne Passwort) ----------------------------------------
Write-Step "Lade Zertifikat aus $CertPath"
$flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($CertPath, [string]$null, $flags)
if (-not $cert.HasPrivateKey) {
    throw "Die PFX enthaelt keinen privaten Schluessel."
}
Write-OK "Zertifikat: $($cert.Subject), Thumbprint $($cert.Thumbprint)"

# -- Anmelden -------------------------------------------------------------------
Write-Step "Verbinde mit Exchange Online (app-only) fuer $Organization"
Connect-ExchangeOnline -AppId $AppId -Certificate $cert -Organization $Organization `
    -ShowBanner:$false -ShowProgress:$false
Write-OK "Verbunden"

try {
    $ips = @()
    if ($SenderIPAddresses -ne "") {
        $ips = @($SenderIPAddresses.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })
    }
    $perAdresse = ($ips.Count -gt 0)

    Write-Step "Pruefe Inbound-Connector '$ConnectorName'"
    $vorhanden = Get-InboundConnector -Identity $ConnectorName -ErrorAction SilentlyContinue

    if ($perAdresse) {
        if ($vorhanden) {
            Write-Warn "Connector vorhanden - aktualisiere (Adressvariante)"
            Set-InboundConnector -Identity $ConnectorName `
                -SenderIPAddresses $ips -RequireTls $false `
                -TlsSenderCertificateName $null -Enabled $true -Comment $managedBy
        } else {
            New-InboundConnector -Name $ConnectorName -ConnectorType OnPremises `
                -SenderDomains @("*") -SenderIPAddresses $ips -RequireTls $false `
                -Enabled $true -Comment $managedBy | Out-Null
        }
        Write-OK "Inbound-Connector erkennt das Relay an: $($ips -join ', ')"
    } else {
        if ($vorhanden) {
            Write-Warn "Connector vorhanden - aktualisiere (Zertifikatsvariante)"
            Set-InboundConnector -Identity $ConnectorName `
                -RequireTls $true -TlsSenderCertificateName $RelayHostname `
                -Enabled $true -Comment $managedBy
        } else {
            New-InboundConnector -Name $ConnectorName -ConnectorType OnPremises `
                -SenderDomains @("*") -RequireTls $true `
                -TlsSenderCertificateName $RelayHostname `
                -Enabled $true -Comment $managedBy | Out-Null
        }
        Write-OK "Inbound-Connector erkennt das Relay am Zertifikat: $RelayHostname"
        Write-Warn "Zertifikatsvariante: Exchange Online akzeptiert nur Zertifikate einer oeffentlichen CA."
    }
}
finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
}

Write-OK "Fertig."
Write-Host ""
Write-Host "Mailfluss:  Geraet -> Relay:25 -> $Organization Smarthost -> [$ConnectorName] -> Zustellung"
