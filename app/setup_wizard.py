"""Einrichtung über Microsoft Graph — nach der Anmeldung als Entra-Administrator.

Sieht nach einem Klick aus, tut im Hintergrund fünf Dinge (aus dem Gateway
übernommen, auf das Relay zugeschnitten):

  1. Tenant erkennen: Erstdomäne und daraus den Smarthost.
  2. App-Registrierung „<Name>" anlegen oder wiederverwenden — mit genau EINER
     Berechtigung: `Exchange.ManageAsApp`. Kein Graph, kein Geheimnis: Der
     Dienst spricht Exchange nur per PowerShell mit Zertifikat.
  3. Zustimmung erteilen und die Rolle Exchange-Administrator zuweisen.
  4. Auth-Zertifikat erzeugen (`auth_cert.py`) und den öffentlichen Teil an die
     App-Registrierung hängen.
  5. Die Rückadresse dieses Dienstes an der Login-App nachtragen, damit der
     nächste Login ohne Copy-Paste läuft.

Alle Aufrufe sind idempotent: Ein zweiter Lauf (etwa nach einem Fehler) ändert
nichts, was schon stimmt, und erneuert nur das Zertifikat.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone

import auth_cert
import config
import settings_store

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
_EXO_APP_ID = "00000002-0000-0ff1-ce00-000000000000"                  # Exchange Online
_EXO_MANAGE_AS_APP = "dc50a0fb-09a3-484d-be87-e023b12c6440"           # Exchange.ManageAsApp
_EXCHANGE_ADMIN_ROLE_ID = "29232cdf-9323-42fd-ade2-1d097af3e4de"      # in jedem Tenant gleich

# In Tests austauschbar (httpx.MockTransport).
_transport = None


async def _gh(method: str, url: str, token: str, **kwargs) -> dict:
    import httpx
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30, transport=_transport) as client:
        resp = await getattr(client, method)(url, headers=headers, **kwargs)
    if not resp.is_success:
        raise RuntimeError(f"Graph {method.upper()} {url} → {resp.status_code}: {resp.text[:400]}")
    return resp.json() if resp.content else {}


def _odata(text: str) -> str:
    return text.replace("'", "''")


# ── 1. Tenant ────────────────────────────────────────────────────────────────

async def discover_tenant(token: str) -> dict:
    data = await _gh("get", f"{GRAPH}/organization?$select=id,verifiedDomains", token)
    orgs = data.get("value", [])
    if not orgs:
        raise RuntimeError("Keine Organisation im Token")
    org = orgs[0]
    domains = org.get("verifiedDomains", [])
    initial = next((d["name"] for d in domains if d.get("isInitial")), None)
    if not initial:
        raise RuntimeError("Erstdomäne (.onmicrosoft.com) nicht bestimmbar")
    smarthost = initial.replace(".onmicrosoft.com", ".mail.protection.outlook.com")
    return {"tenant_id": org["id"], "tenant_domain": initial, "smarthost": smarthost}


# ── 2./3. App-Registrierung, Zustimmung, Rolle ───────────────────────────────

async def _sp_id_for(token: str, app_id: str) -> str:
    data = await _gh("get", f"{GRAPH}/servicePrincipals?$filter=appId eq '{app_id}'&$select=id", token)
    items = data.get("value", [])
    if not items:
        raise RuntimeError(f"Service Principal für {app_id} nicht gefunden")
    return items[0]["id"]


async def create_app_registration(token: str) -> dict:
    name = settings_store.get("GATEWAY_NAME") or "EXO SMTP Relay"
    existing = await _gh("get", f"{GRAPH}/applications?$filter=displayName eq '{_odata(name)}'"
                                "&$select=id,appId", token)
    apps = existing.get("value", [])
    if apps:
        obj_id, app_id = apps[0]["id"], apps[0]["appId"]
        log.info("App-Registrierung wiederverwendet: appId=%s", app_id)
    else:
        app = await _gh("post", f"{GRAPH}/applications", token, json={
            "displayName": name,
            "signInAudience": "AzureADMyOrg",
            "requiredResourceAccess": [{
                "resourceAppId": _EXO_APP_ID,
                "resourceAccess": [{"id": _EXO_MANAGE_AS_APP, "type": "Role"}],
            }],
        })
        obj_id, app_id = app["id"], app["appId"]
        log.info("App-Registrierung angelegt: appId=%s", app_id)

    sp = await _gh("get", f"{GRAPH}/servicePrincipals?$filter=appId eq '{app_id}'&$select=id", token)
    items = sp.get("value", [])
    if items:
        sp_id = items[0]["id"]
    else:
        sp_id = (await _gh("post", f"{GRAPH}/servicePrincipals", token, json={"appId": app_id}))["id"]
        log.info("Service Principal angelegt: %s", sp_id)

    # Zustimmung: Exchange.ManageAsApp. Ein zweiter Lauf meldet „already exists" — Warnung, kein Abbruch.
    try:
        exo_sp = await _sp_id_for(token, _EXO_APP_ID)
        await _gh("post", f"{GRAPH}/servicePrincipals/{sp_id}/appRoleAssignments", token,
                  json={"principalId": sp_id, "resourceId": exo_sp, "appRoleId": _EXO_MANAGE_AS_APP})
        log.info("Zustimmung für Exchange.ManageAsApp erteilt")
    except Exception as exc:                                  # noqa: BLE001
        log.warning("Zustimmung nicht erteilt (bereits vorhanden?): %s", exc)
    try:
        await _gh("post", f"{GRAPH}/roleManagement/directory/roleAssignments", token,
                  json={"principalId": sp_id, "roleDefinitionId": _EXCHANGE_ADMIN_ROLE_ID,
                        "directoryScopeId": "/"})
        log.info("Rolle Exchange-Administrator zugewiesen")
    except Exception as exc:                                  # noqa: BLE001
        log.warning("Rolle nicht zugewiesen (bereits vorhanden?): %s", exc)
    return {"app_id": app_id, "app_object_id": obj_id, "sp_id": sp_id}


# ── 4. Zertifikat ────────────────────────────────────────────────────────────

async def _upload_key_credential(token: str, app_object_id: str, cert_der: bytes) -> None:
    ende = (datetime.now(timezone.utc) + timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _gh("patch", f"{GRAPH}/applications/{app_object_id}", token, json={
        "keyCredentials": [{"type": "AsymmetricX509Cert", "usage": "Verify",
                            "key": base64.b64encode(cert_der).decode(), "endDateTime": ende}]})


# ── 5. Rückadresse an der Login-App ──────────────────────────────────────────

def sso_redirect_uri() -> str:
    """Die öffentliche Rückadresse dieses Dienstes."""
    host = (settings_store.get("PUBLIC_HOSTNAME") or "").strip()
    if not host:
        return ""
    port = config.WEBUI_PORT
    return f"https://{host}{'' if port == 443 else f':{port}'}/auth/callback"


def localhost_redirect_uri() -> str:
    return f"http://localhost:{config.WEBUI_PORT}/auth/callback"


async def patch_bootstrap_redirect_uri(token: str) -> None:
    bootstrap = (settings_store.get("BOOTSTRAP_CLIENT_ID") or "").strip()
    uri = sso_redirect_uri()
    if not bootstrap or not uri:
        return
    try:
        resp = await _gh("get", f"{GRAPH}/applications?$filter=appId eq '{bootstrap}'"
                                "&$select=id,publicClient", token)
        apps = resp.get("value", [])
        if not apps:
            log.warning("Login-App %s nicht im Verzeichnis gefunden", bootstrap)
            return
        uris = list(apps[0].get("publicClient", {}).get("redirectUris", []))
        if uri not in uris:
            uris.append(uri)
            await _gh("patch", f"{GRAPH}/applications/{apps[0]['id']}", token,
                      json={"publicClient": {"redirectUris": uris}})
            log.info("Rückadresse %s an der Login-App nachgetragen", uri)
        settings_store.update({"BOOTSTRAP_REDIRECT_URIS": uris})
    except Exception as exc:                                  # noqa: BLE001
        log.warning("Login-App nicht nachgetragen: %s", exc)


# ── Alles zusammen ───────────────────────────────────────────────────────────

async def run_post_auth_setup(token: str) -> dict:
    tenant = await discover_tenant(token)
    patch = {"TENANT_ID": tenant["tenant_id"], "TENANT_DOMAIN": tenant["tenant_domain"]}
    if not (settings_store.get("EXO_SMARTHOST") or "").strip():
        patch["EXO_SMARTHOST"] = tenant["smarthost"]
    settings_store.update(patch)

    app = await create_app_registration(token)
    settings_store.update({"CLIENT_ID": app["app_id"]})

    ergebnis = {"tenant": tenant, "app_id": app["app_id"]}
    try:
        auth_cert.erzeugen()
        await _upload_key_credential(token, app["app_object_id"], auth_cert.public_cer())
        settings_store.update({"AZURE_APP_CREATED": True})
        ergebnis["auth_cert"] = auth_cert.info()
        log.info("Auth-Zertifikat erzeugt und hochgeladen")
    except Exception as exc:                                  # noqa: BLE001
        log.error("Auth-Zertifikat: %s", exc)
        ergebnis["auth_cert_error"] = str(exc)

    await patch_bootstrap_redirect_uri(token)
    return ergebnis
