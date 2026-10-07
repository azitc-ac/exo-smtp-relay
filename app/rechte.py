"""Rechte der App: einrichten mit Admin, betreiben mit Lesen.

ANLASS (07.10.2026)
-------------------
Die App des Relays bekam bei der Einrichtung die Entra-Rolle
**Exchange-Administrator** — tenantweit, dauerhaft, mit einem Schlüssel ohne
Kennwort auf der Platte. Gebraucht wird sie nur EINMAL: zum Anlegen des
Inbound-Connectors. Im Betrieb liest das Relay nur die Postfachliste.
Wer die `auth.pfx` kopiert, konnte sich bis dahin von überall als
Exchange-Administrator anmelden.

DER WEG
-------
1. Einrichtung wie bisher mit Exchange-Administrator (Connector anlegen).
2. Danach — solange die Admin-Anmeldung noch frisch ist — `herabstufen()`:
   a) EXO-Service-Principal + Rollengruppe „<Name> - nur lesen" mit
      `View-Only Recipients` (Postfachliste) und `View-Only Configuration`
      (Verbindungstest, Connector-Prüfung) — als App selbst, sie ist ja noch
      Admin;
   b) die Entra-Rolle entfernen — mit dem ADMIN-Token, denn eine App kann ihre
      eigene Rolle nicht entfernen.
3. Spätere Änderungen am Connector: `hochstufen()` nach erneuter Anmeldung,
   danach wieder `herabstufen()`.

Primärquelle: learn.microsoft.com/powershell/exchange/app-only-auth-powershell-v2,
„Option 2: Assign custom role groups to the application using service
principals".

⚠️ EXCHANGE ZIEHT DAS HERABSTUFEN NICHT SOFORT NACH (live 07.10.2026): Die
Entra-Rolle war weg, das frische Token der App ohne Admin-Rolle, in Exchange
nur noch die Lesegruppe zugewiesen — und trotzdem schrieb die App 3 h 20 min
später noch erfolgreich (`Set-Mailbox`, sofort zurückgesetzt), auch nach
38 Minuten ohne jeden Aufruf. Eine Frist dafür dokumentiert Microsoft nicht
(der 30-Minuten-bis-2-Stunden-Cache in „RBAC for Applications" betrifft
Graph/EWS-Rollen). Deshalb meldet `herabstufen()` keinen Vollzug, sondern
`nachmessen()` prüft stündlich, bis die Schreibrechte wirklich weg sind, und
hält fest, nach wie vielen Minuten (`RECHTE_WIRKSAM`).

⚠️ NUR FÜR EINE EIGENE APP. Über `auth_cert.importieren()` kann ein Betreiber
die App des Signatur-Gateways mitbenutzen. Die braucht im Betrieb Schreibrechte
(Verteiler, Transportregeln) — sie herabzustufen legte das Gateway lahm.
`APP_EIGEN` setzt nur der Assistent, der die App selbst angelegt hat.

⚠️ DAS ADMIN-TOKEN liegt nur im Speicher, nie auf der Platte, und nur bis zu
seinem Ablauf (Microsoft: rund eine Stunde). Ist es weg, bleibt die App Admin,
bis sich jemand erneut anmeldet — die Oberfläche zeigt das an, statt es zu
verschweigen.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import config
import settings_store

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
ROLLE_EXCHANGE_ADMIN = "29232cdf-9323-42fd-ade2-1d097af3e4de"   # in jedem Tenant gleich
LESEROLLEN = ("View-Only Recipients", "View-Only Configuration")

# Cmdlets, deren Vorhandensein in der Sitzung „Admin" bedeutet — eines je
# Bereich, den das Relay als Admin berühren könnte.
SCHREIB_CMDLETS = ("New-InboundConnector", "New-TransportRule", "Set-Mailbox")

_admin: dict = {"token": None, "bis": 0.0}


# ── Admin-Token (nur Speicher) ───────────────────────────────────────────────

def merke_admin_token(token: str, gueltig_s: int | float | None) -> None:
    # Fünf Minuten Puffer: Ein Token, das mitten im Herabstufen abläuft, hinterliesse
    # eine halb umgebaute App (Lesegruppe da, Admin-Rolle noch da).
    dauer = float(gueltig_s or 3000) - 300
    _admin.update(token=token, bis=time.monotonic() + max(dauer, 0))


def admin_token() -> str | None:
    if _admin["token"] and time.monotonic() < _admin["bis"]:
        return _admin["token"]
    _admin.update(token=None, bis=0.0)
    return None


def vergiss_admin_token() -> None:
    _admin.update(token=None, bis=0.0)


# ── Voraussetzung ────────────────────────────────────────────────────────────

def eigene_app() -> tuple[bool, str]:
    if not (settings_store.get("CLIENT_ID") or "").strip():
        return False, "Keine App-Registrierung eingetragen."
    if not settings_store.get("APP_EIGEN"):
        return False, ("Die App wurde nicht von diesem Assistenten angelegt (z. B. "
                       "Zertifikat des Signatur-Gateways importiert) — ihre Rechte "
                       "werden hier nicht verändert.")
    if not (settings_store.get("APP_SP_ID") or "").strip():
        return False, "Kennung des Dienstprinzipals fehlt — bitte einmal neu anmelden."
    return True, ""


def gruppenname() -> str:
    name = (settings_store.get("GATEWAY_NAME") or "EXO SMTP Relay").strip()
    return f"{name} - nur lesen"


def _merke(ok: bool, text: str, stufe: str = "") -> dict:
    lauf = {"zeit": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ok": bool(ok), "text": str(text)[:400]}
    if stufe:
        lauf["stufe"] = stufe
    settings_store.update({"RECHTE_LAUF": lauf})
    (log.info if ok else log.warning)("Rechte: %s", text)
    return lauf


# ── Exchange-Seite (als App, per PowerShell) ─────────────────────────────────

def _ps(body: str, timeout: int = 300) -> dict:
    import exo_setup
    return exo_setup._run_inline(exo_setup._verbindung_kopf() + "\n" + body, timeout=timeout)


def _q(s: str) -> str:
    """Für einfache Anführungszeichen in PowerShell."""
    return str(s).replace("'", "''")


def lesegruppe_einrichten() -> dict:
    """Service-Principal in Exchange und Rollengruppe „nur lesen" — idempotent.

    Läuft als die App selbst und braucht dafür noch ihre Admin-Rechte. Danach
    hängt das Lesen nicht mehr an der Entra-Rolle."""
    ok, grund = eigene_app()
    if not ok:
        return {"ok": False, "text": grund}
    app_id = settings_store.get("CLIENT_ID").strip()
    sp_id = settings_store.get("APP_SP_ID").strip()
    name = gruppenname()
    rollen = ",".join(f"'{_q(r)}'" for r in LESEROLLEN)
    r = _ps("\n".join([
        f"$sp = Get-ServicePrincipal -Identity '{_q(app_id)}' -ErrorAction SilentlyContinue",
        "if ($null -eq $sp) {",
        f"  New-ServicePrincipal -AppId '{_q(app_id)}' -ObjectId '{_q(sp_id)}'"
        f" -DisplayName '{_q(name)}' | Out-Null",
        f"  $sp = Get-ServicePrincipal -Identity '{_q(app_id)}'",
        "}",
        f"$g = Get-RoleGroup -Identity '{_q(name)}' -ErrorAction SilentlyContinue",
        "if ($null -eq $g) {",
        f"  New-RoleGroup -Name '{_q(name)}' -Roles @({rollen}) -Members $sp.Identity"
        " -Description 'EXO SMTP Relay: Postfachliste lesen. Angelegt vom Einrichtungsassistenten.' | Out-Null",
        "} else {",
        f"  foreach ($r in @({rollen})) {{",
        f"    if (-not (Get-ManagementRoleAssignment -RoleAssignee '{_q(name)}' -Role $r -ErrorAction SilentlyContinue)) {{",
        f"      New-ManagementRoleAssignment -SecurityGroup '{_q(name)}' -Role $r | Out-Null }} }}",
        f"  $m = @(Get-RoleGroupMember -Identity '{_q(name)}' | ForEach-Object {{ \"$($_.Name)\" }})",
        f"  if ($m -notcontains \"$($sp.Name)\" -and $m -notcontains '{_q(sp_id)}') {{",
        f"    Add-RoleGroupMember -Identity '{_q(name)}' -Member $sp.Identity }}",
        "}",
        "Write-Output 'LESEGRUPPE-OK'",
        "Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null",
    ]))
    if r.get("ok") and "LESEGRUPPE-OK" in r.get("output", ""):
        settings_store.update({"RECHTE_LESEGRUPPE": name})
        return {"ok": True, "text": f"Rollengruppe „{name}“ steht ({' + '.join(LESEROLLEN)})."}
    return {"ok": False, "text": "Rollengruppe nicht angelegt: " + (r.get("output") or "")[-500:]}


def messen() -> dict:
    """Was kann die App JETZT? Gemessen in einer frischen Sitzung, nicht gemerkt.

    stufe: "admin" (Schreib-Cmdlets vorhanden) · "lesen" (Postfachliste ja,
    Schreiben nein) · "keine" (Anmeldung geht, Lesen nicht) · "fehler"."""
    import exo_setup
    ok, grund = exo_setup._voraussetzungen()
    if not ok:
        return {"stufe": "fehler", "text": grund}
    pruef = ",".join(f"'{c}'" for c in SCHREIB_CMDLETS)
    r = _ps("\n".join([
        f"$s = @(@({pruef}) | Where-Object {{ Get-Command $_ -ErrorAction SilentlyContinue }})",
        "$lesen = $false",
        "try { $null = Get-EXOMailbox -ResultSize 1 -ErrorAction Stop; $lesen = $true } catch { }",
        "Write-Output ('RECHTE schreib=' + ($s -join '/') + ' lesen=' + $lesen)",
        "Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null",
    ]), timeout=180)
    zeile = next((z for z in (r.get("output") or "").splitlines() if z.startswith("RECHTE ")), "")
    if not zeile:
        return {"stufe": "fehler", "text": (r.get("output") or "keine Antwort")[-400:]}
    schreib = zeile.split("schreib=", 1)[1].split(" lesen=", 1)[0].strip()
    lesen = zeile.rstrip().endswith("lesen=True")
    if schreib:
        m = {"stufe": "admin", "text": f"Schreibrechte vorhanden ({schreib})."}
    elif lesen:
        m = {"stufe": "lesen", "text": "Nur Lesen: Postfachliste ja, Schreib-Cmdlets keine."}
    else:
        m = {"stufe": "keine", "text": "Angemeldet, aber die Postfachliste ist nicht lesbar."}
    _messung_merken(m)
    return m


def _messung_merken(m: dict) -> None:
    """Jede Messung festhalten; die erste ohne Schreibrecht nach dem Herabstufen
    ist der Zeitpunkt, ab dem es WIRKT — mit der Dauer seit dem Herabstufen."""
    jetzt = datetime.now(timezone.utc)
    neu = {"RECHTE_MESSUNG": {"zeit": jetzt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                              "stufe": m["stufe"], "text": m["text"][:200]}}
    herab = settings_store.get("RECHTE_HERABGESTUFT") or ""
    if herab and m["stufe"] in ("lesen", "keine") and not settings_store.get("RECHTE_WIRKSAM"):
        try:
            t0 = datetime.strptime(herab, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            minuten = int((jetzt - t0).total_seconds() // 60)
        except ValueError:
            minuten = None
        neu["RECHTE_WIRKSAM"] = {"zeit": neu["RECHTE_MESSUNG"]["zeit"], "minuten": minuten}
        log.info("Rechte: Herabstufen wirkt in Exchange — %s Minuten nach dem Herabstufen", minuten)
    settings_store.update(neu)


def nachmessen() -> dict | None:
    """Stundenlauf: nach dem Herabstufen messen, bis Exchange es übernommen hat.
    Danach nichts mehr — eine Messung kostet eine PowerShell-Sitzung."""
    if not eigene_app()[0] or not settings_store.get("RECHTE_HERABGESTUFT"):
        return None
    if settings_store.get("RECHTE_WIRKSAM"):
        return None
    return messen()


# ── Entra-Seite (mit Admin-Token, per Graph) ─────────────────────────────────

async def _gh(method: str, url: str, token: str, **kw) -> dict:
    import setup_wizard
    return await setup_wizard._gh(method, url, token, **kw)


async def _zuweisungen(token: str) -> list[dict]:
    sp_id = settings_store.get("APP_SP_ID").strip()
    url = (f"{GRAPH}/roleManagement/directory/roleAssignments?$filter=principalId eq "
           f"'{sp_id}' and roleDefinitionId eq '{ROLLE_EXCHANGE_ADMIN}'")
    return (await _gh("get", url, token)).get("value", [])


async def adminrolle_zuweisen(token: str) -> dict:
    ok, grund = eigene_app()
    if not ok:
        return _merke(False, grund)
    if await _zuweisungen(token):
        settings_store.update({"RECHTE_HERABGESTUFT": "", "RECHTE_WIRKSAM": {}})
        return _merke(True, "Exchange-Administrator war bereits zugewiesen.", "admin")
    await _gh("post", f"{GRAPH}/roleManagement/directory/roleAssignments", token,
              json={"principalId": settings_store.get("APP_SP_ID").strip(),
                    "roleDefinitionId": ROLLE_EXCHANGE_ADMIN, "directoryScopeId": "/"})
    settings_store.update({"RECHTE_HERABGESTUFT": "", "RECHTE_WIRKSAM": {}})
    return _merke(True, "Exchange-Administrator zugewiesen — für Änderungen am Connector. "
                        "Danach wieder herabstufen.", "admin")


async def adminrolle_entfernen(token: str) -> int:
    weg = 0
    for z in await _zuweisungen(token):
        await _gh("delete", f"{GRAPH}/roleManagement/directory/roleAssignments/{z['id']}", token)
        weg += 1
    return weg


async def herabstufen(token: str | None = None) -> dict:
    """Lesegruppe sichern, DANN die Admin-Rolle entfernen — nie umgekehrt.

    Umgekehrt stünde die App ohne jedes Recht da, wenn die Rollengruppe
    scheitert, und könnte sie mangels Rechten auch nicht mehr anlegen."""
    import asyncio
    ok, grund = eigene_app()
    if not ok:
        return _merke(False, grund)
    token = token or admin_token()
    if not token:
        return _merke(False, "Admin-Anmeldung abgelaufen — die App hat weiter "
                             "Exchange-Administrator. Zum Herabstufen kurz neu anmelden.", "admin")
    gruppe = await asyncio.to_thread(lesegruppe_einrichten)
    if not gruppe["ok"]:
        return _merke(False, gruppe["text"] + " — Admin-Rolle bleibt deshalb bestehen.", "admin")
    try:
        weg = await adminrolle_entfernen(token)
    except Exception as exc:                                  # noqa: BLE001
        return _merke(False, f"Rollengruppe steht, aber die Admin-Rolle liess sich nicht "
                             f"entfernen: {exc}", "admin")
    vergiss_admin_token()
    settings_store.update({"RECHTE_HERABGESTUFT": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "RECHTE_WIRKSAM": {}})
    text = (f"Herabgestuft: Exchange-Administrator in Entra entfernt ({weg} Zuweisung"
            f"{'en' if weg != 1 else ''}), Lesen über „{gruppenname()}“. Exchange übernimmt das "
            "verzögert — beobachtet wurden über drei Stunden. Der Dienst misst stündlich nach "
            "und zeigt hier, ab wann die Schreibrechte wirklich weg sind.")
    return _merke(True, text, "lesen")


def zustand() -> dict:
    """Für die Oberfläche — ohne Messung (die kostet eine PowerShell-Sitzung)."""
    ok, grund = eigene_app()
    return {"eigene_app": ok, "hinweis": grund,
            "lesegruppe": settings_store.get("RECHTE_LESEGRUPPE") or "",
            "herabgestuft": settings_store.get("RECHTE_HERABGESTUFT") or "",
            "messung": settings_store.get("RECHTE_MESSUNG") or {},
            "wirksam": settings_store.get("RECHTE_WIRKSAM") or {},
            "lauf": settings_store.get("RECHTE_LAUF") or {},
            "admin_token": admin_token() is not None}
