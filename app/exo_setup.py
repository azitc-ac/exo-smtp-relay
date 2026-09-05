"""Exchange Online per PowerShell: Verbindung prüfen, Inbound-Connector anlegen.

Alles hier läuft ausserhalb des Mailpfads, auf Knopfdruck aus der Oberfläche,
und dauert Sekunden bis eine Minute (Modul laden, anmelden). Die Skripte
liegen unter `scripts/` und sind PowerShell-5.1-tauglich, weil sie unter
Windows auch in der eingebauten Shell laufen müssen.

WARUM IP ODER ZERTIFIKAT
------------------------
Ein Inbound-Connector erkennt den Absender entweder am TLS-Zertifikat
(`TlsSenderCertificateName`) oder an der Quelladresse (`SenderIPAddresses`).
Exchange Online akzeptiert für die Zertifikatsvariante nur Zertifikate einer
öffentlichen CA — ein selbstsigniertes reicht dort NICHT. Wer keines hat, nimmt
die Adressvariante; die braucht eine feste öffentliche IP. Beides ist in der
Oberfläche wählbar, und das Skript bekommt genau eines von beiden.
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path

import auth_cert
import config
import settings_store

log = logging.getLogger(__name__)

SCRIPTS = config.APP_DIR / "scripts"


def _voraussetzungen() -> tuple[bool, str]:
    app_id = (settings_store.get("CLIENT_ID") or "").strip()
    org = (settings_store.get("TENANT_DOMAIN") or "").strip()
    if not app_id:
        return False, "App-ID (Client-ID) fehlt"
    if not org:
        return False, "Tenant-Domäne fehlt"
    if not auth_cert.PFX_PATH.exists():
        return False, "Auth-Zertifikat fehlt — zuerst erzeugen und in Entra hochladen"
    return True, ""


def _run(args: list[str], timeout: int = 300) -> dict:
    cmd = [config.PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {"ok": False, "output": f"PowerShell nicht gefunden: {config.PWSH}. "
                                       "Unter Linux `pwsh` installieren, unter Windows "
                                       "genügt die eingebaute PowerShell 5.1."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"Zeitüberschreitung nach {timeout} s"}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "output": str(exc)}
    out = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    return {"ok": proc.returncode == 0, "output": out, "rc": proc.returncode}


def _run_inline(body: str, timeout: int = 300) -> dict:
    """Skripttext in eine Datei — `-Command` mit mehrzeiligem Text ist unter
    PS 5.1 unzuverlässig, und Anführungszeichen würden doppelt maskiert."""
    with tempfile.NamedTemporaryFile(suffix=".ps1", mode="w", delete=False,
                                     encoding="utf-8-sig") as f:
        f.write(body)
        pfad = f.name
    try:
        return _run(["-File", pfad], timeout=timeout)
    finally:
        try:
            Path(pfad).unlink()
        except OSError:
            pass


def _verbindung_kopf() -> str:
    app_id = settings_store.get("CLIENT_ID").strip()
    org = settings_store.get("TENANT_DOMAIN").strip()
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "Import-Module ExchangeOnlineManagement",
        "$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(",
        f"    '{auth_cert.PFX_PATH}', [string]$null,",
        "    [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)",
        f"Connect-ExchangeOnline -AppId '{app_id}' -Certificate $cert -Organization '{org}'"
        " -ShowBanner:$false -ShowProgress:$false | Out-Null",
    ])


def modul_pruefen() -> dict:
    """Ist PowerShell da, ist das Modul da? Ohne Anmeldung."""
    r = _run_inline("\n".join([
        "$m = Get-Module -ListAvailable ExchangeOnlineManagement | Sort-Object Version -Descending | Select-Object -First 1",
        "if ($null -eq $m) { Write-Output 'MODUL-FEHLT'; exit 2 }",
        "Write-Output ('ExchangeOnlineManagement ' + $m.Version.ToString() + ' / PowerShell ' + $PSVersionTable.PSVersion.ToString())",
    ]), timeout=120)
    if r["ok"]:
        return {"ok": True, "text": r["output"].splitlines()[-1] if r["output"] else "vorhanden"}
    if "MODUL-FEHLT" in r.get("output", ""):
        return {"ok": False, "text": "Modul ExchangeOnlineManagement fehlt — "
                                     "`Install-Module ExchangeOnlineManagement -Scope AllUsers`"}
    return {"ok": False, "text": r.get("output") or "PowerShell nicht ausführbar"}


def verbindung_testen() -> dict:
    ok, grund = _voraussetzungen()
    if not ok:
        return {"ok": False, "text": grund}
    r = _run_inline(_verbindung_kopf() + "\n" + "\n".join([
        "$o = Get-OrganizationConfig",
        "Write-Output ('VERBUNDEN ' + $o.DisplayName)",
        "Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null",
    ]))
    if r["ok"] and "VERBUNDEN" in r["output"]:
        zeile = [z for z in r["output"].splitlines() if z.startswith("VERBUNDEN")][-1]
        return {"ok": True, "text": "Angemeldet als Anwendung — Organisation: " + zeile[10:].strip()}
    return {"ok": False, "text": (r.get("output") or "unbekannter Fehler")[-600:]}


def connector_einrichten(hostname: str, sender_ips: list[str] | None = None) -> dict:
    """Inbound-Connector anlegen oder nachziehen (idempotent)."""
    ok, grund = _voraussetzungen()
    if not ok:
        return {"ok": False, "output": grund}
    skript = SCRIPTS / "setup_relay_connector.ps1"
    if not skript.exists():
        return {"ok": False, "output": f"Skript fehlt: {skript}"}
    name = settings_store.get("GATEWAY_NAME") or "EXO SMTP Relay"
    args = ["-File", str(skript),
            "-AppId", settings_store.get("CLIENT_ID").strip(),
            "-Organization", settings_store.get("TENANT_DOMAIN").strip(),
            "-CertPath", str(auth_cert.PFX_PATH),
            "-RelayHostname", hostname.strip(),
            "-ConnectorName", f"{name} - Inbound"]
    ips = [i.strip() for i in (sender_ips or []) if i.strip()]
    if ips:
        args += ["-SenderIPAddresses", ",".join(ips)]
    r = _run(args)
    if r["ok"]:
        settings_store.update({"EXO_CONNECTOR_CREATED": True})
        log.info("Inbound-Connector eingerichtet")
    else:
        log.error("Inbound-Connector fehlgeschlagen: %s", r.get("output", "")[-400:])
    return r


def connector_pruefen() -> dict:
    ok, grund = _voraussetzungen()
    if not ok:
        return {"ok": False, "text": grund}
    name = settings_store.get("GATEWAY_NAME") or "EXO SMTP Relay"
    r = _run_inline(_verbindung_kopf() + "\n" + "\n".join([
        f"$c = Get-InboundConnector -Identity '{name} - Inbound' -ErrorAction SilentlyContinue",
        "if ($null -eq $c) { Write-Output 'CONNECTOR-FEHLT' } else {",
        "  $o = [ordered]@{ Name=$c.Name; Enabled=$c.Enabled; RequireTls=$c.RequireTls;",
        "    TlsSenderCertificateName=\"$($c.TlsSenderCertificateName)\";",
        "    SenderIPAddresses=@($c.SenderIPAddresses | ForEach-Object { \"$_\" });",
        "    SenderDomains=@($c.SenderDomains | ForEach-Object { \"$_\" }) }",
        "  Write-Output ('CONNECTOR ' + ($o | ConvertTo-Json -Compress)) }",
        "Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null",
    ]))
    out = r.get("output") or ""
    if "CONNECTOR-FEHLT" in out:
        return {"ok": True, "vorhanden": False, "text": "Kein Inbound-Connector für dieses Relay"}
    for zeile in out.splitlines():
        if zeile.startswith("CONNECTOR "):
            try:
                return {"ok": True, "vorhanden": True, "connector": json.loads(zeile[10:])}
            except ValueError:
                break
    return {"ok": False, "text": out[-600:] or "unbekannter Fehler"}
