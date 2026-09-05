"""Der Einrichtungsassistent — wenige Klicks, im Hintergrund die ganze Arbeit.

Sechs Schritte, jeder mit sichtbarem Zustand:

  1. Passwort setzen
  2. Hostname (TLS-Zertifikat wird selbstsigniert darauf ausgestellt)
  3. Entra-Login → App-Registrierung, Zustimmung, Rolle, Zertifikat-Upload,
     Tenant, Smarthost — alles automatisch (`setup_wizard.run_post_auth_setup`)
  4. Inbound-Connector in Exchange Online (PowerShell)
  5. Geräte: Lernmodus oder von Hand
  6. Abschluss

`/` leitet hierher, bis Schritt 6 geklickt ist — wie beim Gateway.
"""
from __future__ import annotations

import asyncio
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import auth_cert
import config
import exo_mailboxes
import exo_setup
import pkce as pkce_mod
import relay_hosts
import settings_store
import setup_wizard
import tls_cert

from webui.deps import (templates, log, _gateway_name, _require_admin, _get_session_user,
                        _password_change_required)

router = APIRouter()


def zustand() -> dict:
    """Was ist erledigt? Eine Quelle für Seite und Live-Aktualisierung."""
    s = settings_store.get_all()
    geraete = relay_hosts.liste()
    tls = tls_cert.info()
    return {
        "password_ok": not _password_change_required(),
        "hostname": s.get("PUBLIC_HOSTNAME") or "",
        "tls": tls,
        "bootstrap_client_id": s.get("BOOTSTRAP_CLIENT_ID") or "",
        "bootstrap_redirect_uris": s.get("BOOTSTRAP_REDIRECT_URIS") or [],
        "sso_redirect_uri": setup_wizard.sso_redirect_uri(),
        "localhost_redirect_uri": setup_wizard.localhost_redirect_uri(),
        "azure_app_created": bool(s.get("AZURE_APP_CREATED")),
        "tenant_domain": s.get("TENANT_DOMAIN") or "",
        "tenant_id": s.get("TENANT_ID") or "",
        "client_id": s.get("CLIENT_ID") or "",
        "smarthost": s.get("EXO_SMARTHOST") or "",
        "auth_cert": auth_cert.info(),
        "exo": exo_mailboxes.zustand(),
        "connector_created": bool(s.get("EXO_CONNECTOR_CREATED")),
        "geraete": len(geraete),
        "setup_complete": bool(s.get("SETUP_COMPLETE")),
        "pwsh": config.PWSH,
        "webui_port": config.WEBUI_PORT,
        "smtp_port": config.SMTP_PORT,
    }


@router.get("/einrichtung", response_class=HTMLResponse)
async def einrichtung_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="einrichtung.html",
        context={"active": "einrichtung", "gateway_name": _gateway_name(), "e": zustand(),
                 "s": settings_store.public_view(),
                 "auth_error": request.query_params.get("auth_error", "")})


@router.get("/api/setup/status")
async def api_setup_status(user: str = Depends(_require_admin)):
    return JSONResponse({"ok": True, **zustand()})


# ── Schritt 2: Hostname ──────────────────────────────────────────────────────

@router.post("/api/setup/hostname")
async def api_setup_hostname(request: Request, user: str = Depends(_require_admin)):
    daten = await request.json()
    hostname = (daten.get("hostname") or "").strip().lower()
    if not hostname or " " in hostname or "/" in hostname:
        return JSONResponse({"ok": False, "error": "Bitte einen Hostnamen wie relay.firma.de angeben."},
                            status_code=400)
    settings_store.update({"PUBLIC_HOSTNAME": hostname})
    # Ein selbstsigniertes Zertifikat gehört auf diesen Namen — ein importiertes
    # bleibt unangetastet, das hat der Betreiber bewusst gewählt.
    st = tls_cert.info()
    neu = False
    if not st.get("vorhanden") or st.get("selbstsigniert") or st.get("fehler"):
        if hostname not in (st.get("hostnames") or []):
            tls_cert.selbstsigniert_erzeugen(hostname)
            neu = True
    log.info("Hostname durch %s auf %s gesetzt%s", user, hostname,
             " — TLS-Zertifikat neu ausgestellt (wirkt nach Neustart)" if neu else "")
    return JSONResponse({"ok": True, "tls_neu": neu, "tls": tls_cert.info()})


# ── Schritt 3: Entra-Login ───────────────────────────────────────────────────

@router.post("/api/setup/bootstrap-client")
async def api_setup_bootstrap_client(request: Request, user: str = Depends(_require_admin)):
    daten = await request.json()
    client_id = (daten.get("client_id") or "").strip()
    if not client_id:
        return JSONResponse({"ok": False, "error": "Client-ID darf nicht leer sein."}, status_code=400)
    settings_store.update({"BOOTSTRAP_CLIENT_ID": client_id})
    log.info("Login-App durch %s gesetzt", user)
    return JSONResponse({"ok": True})


def _setup_redirect_uri(localhost: bool) -> str:
    """HTTPS-Rückadresse, sobald sie an der Login-App bekannt ist — sonst localhost
    (Copy-Paste). Beim allerersten Login ist sie es nie; danach trägt
    `patch_bootstrap_redirect_uri` sie nach, und der zweite Login läuft im Popup."""
    https_uri = setup_wizard.sso_redirect_uri()
    if not localhost and https_uri and https_uri in (settings_store.get("BOOTSTRAP_REDIRECT_URIS") or []):
        return https_uri
    return setup_wizard.localhost_redirect_uri()


@router.get("/auth/start")
async def auth_start(request: Request, user: str = Depends(_require_admin)):
    """Anmeldeadresse als JSON — für den Knopf im Assistenten."""
    localhost = request.query_params.get("localhost") in ("1", "true")
    redirect_uri = _setup_redirect_uri(localhost)
    _state, auth_url = pkce_mod.create_session(redirect_uri)
    return JSONResponse({"auth_url": auth_url, "redirect_uri": redirect_uri,
                         "paste": redirect_uri.startswith("http://localhost")})


def _callback_page(ok: bool, msg: str = "") -> str:
    icon, heading, body, color = (("✓", "Entra-Login abgeschlossen",
                                   "App-Registrierung und Zertifikat eingerichtet. Dieses Fenster schliesst sich …",
                                   "#16a34a") if ok
                                  else ("✗", "Einrichtung fehlgeschlagen", msg or "Unbekannter Fehler", "#dc2626"))
    import json as _json
    post = _json.dumps({"type": "setup-auth-done" if ok else "setup-auth-fail", "msg": msg})
    return f"""<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8"><title>{heading}</title></head>
<body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f8fafc">
<div style="text-align:center;padding:40px;max-width:460px"><div style="font-size:52px;margin-bottom:16px">{icon}</div>
<h2 style="color:{color};margin:0 0 10px">{heading}</h2><p style="color:#64748b;margin:0">{body}</p>
<p style="margin-top:18px"><a href="/einrichtung">Zur Einrichtung</a></p></div>
<script>try{{window.opener&&window.opener.postMessage({post},window.opener.location.origin);}}catch(e){{}}
{'setTimeout(function(){window.close();},1500);' if ok else ''}</script></body></html>"""


class _SitzungAbgelaufen(RuntimeError):
    """Kein Fehler des Dienstes — der Betreiber klickt einfach noch einmal."""


async def _nach_login(code: str, state: str) -> dict:
    sitzung = pkce_mod.pop_session(state)
    if not sitzung:
        raise _SitzungAbgelaufen("Anmeldesitzung abgelaufen — bitte erneut auf „Jetzt anmelden“ klicken.")
    token = (await pkce_mod.exchange_code(code, sitzung["verifier"], sitzung["redirect_uri"]))["access_token"]
    ergebnis = await setup_wizard.run_post_auth_setup(token)
    # Die Postfachliste gleich holen — im Hintergrund, der Login soll nicht darauf warten.
    asyncio.get_running_loop().run_in_executor(None, exo_mailboxes.list_mailboxes, True)
    return ergebnis


@router.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = "",
                        error_description: str = ""):
    """Rückkehr von Microsoft (HTTPS-Weg, im Popup). Ohne Sitzung wird nichts
    ausgeführt — dann bleibt der Copy-Paste-Weg."""
    if error:
        return HTMLResponse(_callback_page(False, f"{error}: {error_description}"))
    if not _get_session_user(request):
        return HTMLResponse(_callback_page(False, "Nicht angemeldet — bitte die Adresse dieser Seite "
                                                  "kopieren und im Assistenten unter „Per Localhost anmelden“ einfügen."))
    try:
        await _nach_login(code, state)
    except Exception as exc:                                  # noqa: BLE001
        log.error("Einrichtung nach Login fehlgeschlagen: %s", exc)
        return HTMLResponse(_callback_page(False, str(exc)))
    return HTMLResponse(_callback_page(True))


@router.post("/api/setup/auth-paste")
async def api_setup_auth_paste(request: Request, user: str = Depends(_require_admin)):
    """Copy-Paste-Weg: Die Adresse, auf der der Browser nach dem Login landete."""
    daten = await request.json()
    try:
        params = urllib.parse.parse_qs(urllib.parse.urlparse((daten.get("url") or "").strip()).query)
    except Exception:                                         # noqa: BLE001
        raise HTTPException(400, "Ungültige Adresse")
    if params.get("error"):
        raise HTTPException(400, f"Microsoft meldet: {params['error'][0]} — "
                                 f"{params.get('error_description', [''])[0]}")
    code, state = params.get("code", [""])[0], params.get("state", [""])[0]
    if not code or not state:
        raise HTTPException(400, "Die Adresse enthält keinen Code. Bitte die vollständige Adresse aus "
                                 f"der Adressleiste kopieren (beginnt mit {setup_wizard.localhost_redirect_uri()}?code=…).")
    try:
        ergebnis = await _nach_login(code, state)
    except _SitzungAbgelaufen as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:                                  # noqa: BLE001
        log.error("Einrichtung nach Login fehlgeschlagen: %s", exc)
        raise HTTPException(500, str(exc))
    log.info("Einrichtung nach Login abgeschlossen durch %s: App %s", user, ergebnis.get("app_id"))
    return JSONResponse({"ok": True, **{k: v for k, v in ergebnis.items() if k != "tenant"}})


# ── Schritt 4: Connector ─────────────────────────────────────────────────────

@router.post("/api/setup/exo-check")
async def api_setup_exo_check(user: str = Depends(_require_admin)):
    modul = await asyncio.to_thread(exo_setup.modul_pruefen)
    if not modul["ok"]:
        return JSONResponse({"ok": False, "modul": modul, "text": modul["text"]})
    anmeldung = await asyncio.to_thread(exo_setup.verbindung_testen)
    return JSONResponse({"ok": anmeldung["ok"], "modul": modul, "text": anmeldung["text"]})


@router.post("/api/setup/connector")
async def api_setup_connector(request: Request, user: str = Depends(_require_admin)):
    daten = await request.json()
    hostname = (settings_store.get("PUBLIC_HOSTNAME") or "").strip()
    if not hostname:
        return JSONResponse({"ok": False, "output": "Erst Schritt 2 (Hostname) erledigen."}, status_code=400)
    ips = daten.get("sender_ips") or ""
    if isinstance(ips, str):
        ips = [t.strip() for t in ips.split(",") if t.strip()]
    r = await asyncio.to_thread(exo_setup.connector_einrichten, hostname, ips)
    log.info("Inbound-Connector über den Assistenten durch %s: %s", user, "ok" if r.get("ok") else "fehlgeschlagen")
    return JSONResponse(r)


# ── Schritt 6: Abschluss ─────────────────────────────────────────────────────

@router.post("/api/setup/mark-complete")
async def api_setup_complete(user: str = Depends(_require_admin)):
    settings_store.update({"SETUP_COMPLETE": True})
    log.info("Einrichtung durch %s abgeschlossen", user)
    return JSONResponse({"ok": True})


@router.post("/api/setup/reopen")
async def api_setup_reopen(user: str = Depends(_require_admin)):
    settings_store.update({"SETUP_COMPLETE": False})
    return JSONResponse({"ok": True})
