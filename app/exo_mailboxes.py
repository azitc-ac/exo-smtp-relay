"""Welche Postfächer gibt es im Tenant? — Exchange Online per PowerShell.

Die Frage entscheidet über beide Relay-Grenzen: Absenderdomäne (aus den
bekannten Adressen abgeleitet) und „nur interne Ziele" (gegen die Adressen
geprüft). Quelle ist `Get-EXOMailbox` über das ExchangeOnlineManagement-Modul
mit Zertifikatsanmeldung — dieselbe Abfrage wie im grossen Gateway, weil sie
die einzige ist, die echte Postfächer samt Aliasen verlässlich liefert.

DREI ABWEICHUNGEN VOM GATEWAY
-----------------------------
1. **Plattencache.** Das Gateway hält die Liste nur im Speicher; nach einem
   Neustart weist es Relay-Post mit 451 ab, bis die erste Abfrage durch ist
   (mehrere Sekunden bis Minuten). Ein Drucker versucht es wieder, ein
   Etikettendrucker von 2011 vielleicht nicht. Hier wird der letzte Stand in
   `mailboxes.json` abgelegt und beim Start sofort geladen.
2. **Handeinträge.** `ADRESSEN_ZUSAETZLICH` aus den Einstellungen zählt mit —
   für Betreiber ohne App-Registrierung, oder für Adressen, die kein Postfach
   sind (Verteiler, Kontakte). `known_addresses()` liefert die Vereinigung.
3. **Shell.** `config.PWSH` statt fest `pwsh`: unter Windows läuft das Modul
   auch in der eingebauten PowerShell 5.1.

⚠️ NIE IM MAILPFAD ABFRAGEN. `known_addresses()` liest nur den Cache. Die
Abfrage selbst läuft im Zeitplaner (stündlich) und auf Knopfdruck.
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import config
import secure_io
import settings_store

log = logging.getLogger("exo_mailboxes")

AUTH_CERT_PATH = Path(config.DATA_DIR) / "auth.pfx"
CACHE_PATH = Path(config.DATA_DIR) / "mailboxes.json"
_TTL = 3600
_lock = threading.RLock()
_cache: list[dict] = []
_cache_ts: float = 0.0
_letzter_fehler: str = ""
_letzter_erfolg: str = ""


def _norm_addresses(raw) -> list[str]:
    """`EmailAddresses` ist eine Liste — ConvertTo-Json macht aus EINEM Eintrag
    aber eine nackte Zeichenkette. Präfix `smtp:` weg, klein, ohne Doppelte."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    seen: set[str] = set()
    out: list[str] = []
    for a in raw:
        s = str(a)
        if s[:5].lower() == "smtp:":
            addr = s[5:].strip().lower()
            if addr and addr not in seen:
                seen.add(addr)
                out.append(addr)
    return out


def _parse_mailboxes(raw_json: str) -> list[dict]:
    try:
        data = json.loads(raw_json)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("EXO-Postfachliste: JSON unlesbar: %s", exc)
        return []
    if isinstance(data, dict):
        data = [data]
    out: list[dict] = []
    for m in data:
        primary = (m.get("primary") or "").strip().lower()
        if not primary:
            continue
        out.append({
            "primary": primary,
            "addresses": _norm_addresses(m.get("addresses")),
            "display_name": m.get("DisplayName") or "",
            "type": m.get("RecipientTypeDetails") or "",
        })
    return out


def _ps_script(app_id: str, org: str) -> str:
    # PS-5.1-tauglich: kein `::new()` mit Flags-Kombination über `-bor` in
    # einem Argument ist nötig; `EphemeralKeySet` gibt es ab .NET 4.7.2 und in
    # PS 7 gleichermassen.
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "Import-Module ExchangeOnlineManagement",
        "$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(",
        f"    '{AUTH_CERT_PATH}', [string]$null,",
        "    [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)",
        f"Connect-ExchangeOnline -AppId '{app_id}' -Certificate $cert -Organization '{org}'"
        " -ShowBanner:$false -ShowProgress:$false | Out-Null",
        "Get-EXOMailbox -RecipientTypeDetails UserMailbox,SharedMailbox -ResultSize Unlimited"
        " -Properties EmailAddresses |",
        "  Select-Object @{n='primary';e={$_.PrimarySmtpAddress}}, DisplayName, RecipientTypeDetails,"
        " @{n='addresses';e={@($_.EmailAddresses | Where-Object {$_ -clike 'smtp:*' -or $_ -clike 'SMTP:*'})}} |",
        "  ConvertTo-Json -Depth 4",
        "Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null",
    ])


def abfrage_moeglich() -> tuple[bool, str]:
    """Sind App-ID, Tenant und Zertifikat da? (ja/nein, Grund)."""
    app_id = (settings_store.get("CLIENT_ID") or "").strip()
    org = (settings_store.get("TENANT_DOMAIN") or "").strip()
    if not app_id:
        return False, "App-ID (CLIENT_ID) fehlt"
    if not org:
        return False, "Tenant-Domäne (TENANT_DOMAIN) fehlt"
    if not AUTH_CERT_PATH.exists():
        return False, f"Auth-Zertifikat fehlt ({AUTH_CERT_PATH})"
    return True, ""


def fetch_mailboxes() -> list[dict]:
    """EXO JETZT abfragen (blockierend, Sekunden). Leer bei jedem Fehler."""
    global _letzter_fehler
    ok, grund = abfrage_moeglich()
    if not ok:
        _letzter_fehler = grund
        log.warning("EXO-Postfachliste übersprungen — %s", grund)
        return []
    app_id = settings_store.get("CLIENT_ID").strip()
    org = settings_store.get("TENANT_DOMAIN").strip()
    with tempfile.NamedTemporaryFile(suffix=".ps1", mode="w", delete=False,
                                     encoding="utf-8-sig") as f:
        f.write(_ps_script(app_id, org))
        ps_path = f.name
    try:
        proc = subprocess.run(
            [config.PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", ps_path],
            capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        _letzter_fehler = f"PowerShell nicht gefunden ({config.PWSH})"
        log.error("EXO-Postfachliste: %s", _letzter_fehler)
        return []
    except Exception as exc:                                  # noqa: BLE001
        _letzter_fehler = str(exc)
        log.error("EXO-Postfachliste fehlgeschlagen: %s", exc)
        return []
    finally:
        try:
            Path(ps_path).unlink()
        except OSError:
            pass
    if proc.returncode != 0:
        _letzter_fehler = (proc.stderr or proc.stdout or "")[-400:].strip()
        log.error("EXO-Postfachliste rc=%s: %s", proc.returncode, _letzter_fehler)
        return []
    out = proc.stdout or ""
    starts = [x for x in (out.find("["), out.find("{")) if x >= 0]
    liste = _parse_mailboxes(out[min(starts):] if starts else out)
    if liste:
        _letzter_fehler = ""
    else:
        _letzter_fehler = "Antwort enthielt keine Postfächer"
    return liste


def _cache_laden() -> None:
    """Letzten Stand von der Platte — einmal, beim ersten Zugriff."""
    global _cache, _cache_ts
    if _cache or not CACHE_PATH.exists():
        return
    try:
        daten = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(daten, dict) and isinstance(daten.get("mailboxes"), list):
            _cache = daten["mailboxes"]
            # Alt genug, dass der Zeitplaner sofort nachlädt — aber vorhanden.
            _cache_ts = 0.0
            log.info("EXO-Postfachliste: %d Postfächer aus %s geladen", len(_cache), CACHE_PATH)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("EXO-Postfachliste: Cache %s unlesbar: %s", CACHE_PATH, exc)


def _cache_schreiben() -> None:
    try:
        secure_io.write_secret_json(CACHE_PATH, {"mailboxes": _cache, "ts": _letzter_erfolg})
    except Exception as exc:                                  # noqa: BLE001
        log.warning("EXO-Postfachliste: Cache nicht geschrieben: %s", exc)


def list_mailboxes(force: bool = False) -> list[dict]:
    """Postfächer aus dem Cache (1 h); bei Bedarf frisch. Fällt bei einem
    Fehlschlag auf den alten Stand zurück — ein Aussetzer bei Microsoft darf
    die Welt nicht leeren."""
    global _cache, _cache_ts, _letzter_erfolg
    with _lock:
        _cache_laden()
        if not force and _cache and (time.monotonic() - _cache_ts) < _TTL:
            return _cache
        mbs = fetch_mailboxes()
        if mbs:
            _cache = mbs
            _cache_ts = time.monotonic()
            _letzter_erfolg = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _cache_schreiben()
        return mbs or _cache


def known_addresses() -> set[str]:
    """Alle bekannten Adressen (Cache + Handeinträge), klein. Nie eine Abfrage.
    Leer heisst „unbekannt", nicht „es gibt keine" — die Aufrufer verweigern
    dann das Relay (siehe `smtp_relay.pruefe`)."""
    with _lock:
        _cache_laden()
        aus: set[str] = set()
        for m in _cache:
            if m.get("primary"):
                aus.add(m["primary"])
            aus.update(m.get("addresses") or [])
    for a in settings_store.get("ADRESSEN_ZUSAETZLICH") or []:
        a = str(a).strip().lower()
        if "@" in a:
            aus.add(a)
    return aus


def zustand() -> dict:
    """Für die Oberfläche: Woher kommen die Adressen, wie alt sind sie?"""
    with _lock:
        _cache_laden()
        anzahl = len(_cache)
    ok, grund = abfrage_moeglich()
    return {
        "postfaecher": anzahl,
        "handeintraege": len(settings_store.get("ADRESSEN_ZUSAETZLICH") or []),
        "letzter_erfolg": _letzter_erfolg,
        "letzter_fehler": _letzter_fehler,
        "abfrage_moeglich": ok,
        "abfrage_hinweis": grund,
        "cache_datei": CACHE_PATH.exists(),
    }


def invalidate() -> None:
    global _cache_ts
    with _lock:
        _cache_ts = 0.0
