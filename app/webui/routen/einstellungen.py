"""Einstellungen: Rückweg, Tenant, Adressquelle, Zertifikate, Connector.

Alle Schreibwege gehen über `settings_store.nur_bekannte()`: Unbekanntes und
Festes (`REINJECT_MODE`) wird gemeldet, nicht still geschluckt.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

import auth_cert
import config
import exo_mailboxes
import exo_setup
import settings_store
import smarthost
import tls_cert

from webui.deps import templates, log, _gateway_name, _require_admin

router = APIRouter()


@router.get("/einstellungen", response_class=HTMLResponse)
async def einstellungen_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="einstellungen.html",
        context={"active": "einstellungen", "gateway_name": _gateway_name(),
                 "s": settings_store.public_view(),
                 "tls": tls_cert.info(), "auth": auth_cert.info(),
                 "exo": exo_mailboxes.zustand(),
                 "pwsh": config.PWSH, "data_dir": config.DATA_DIR,
                 "smtp_port": config.SMTP_PORT})


@router.post("/api/einstellungen")
async def api_einstellungen(request: Request, user: str = Depends(_require_admin)):
    daten = await request.json()
    uebernommen, verworfen = settings_store.nur_bekannte(daten)
    # Ein maskiertes Geheimnis, das unverändert zurückkommt, ist keine Änderung.
    for k in settings_store.SECRET_KEYS:
        if uebernommen.get(k) == settings_store.MASK:
            uebernommen.pop(k)
    if not uebernommen and verworfen:
        return JSONResponse({"ok": False, "error": "Unbekannte Einstellungen: " + ", ".join(verworfen)},
                            status_code=400)
    settings_store.update(uebernommen)
    if "ADRESSEN_ZUSAETZLICH" in uebernommen:
        exo_mailboxes.invalidate()
    log.info("Einstellungen durch %s geändert: %s", user, ", ".join(sorted(uebernommen)))
    return JSONResponse({"ok": True, "verworfen": verworfen})


# ── Adressquelle ─────────────────────────────────────────────────────────────

@router.get("/api/exo/zustand")
async def api_exo_zustand(user: str = Depends(_require_admin)):
    return JSONResponse({"ok": True, **exo_mailboxes.zustand()})


@router.post("/api/exo/abfragen")
async def api_exo_abfragen(user: str = Depends(_require_admin)):
    liste = await asyncio.to_thread(exo_mailboxes.list_mailboxes, True)
    z = exo_mailboxes.zustand()
    return JSONResponse({"ok": bool(liste) and not z["letzter_fehler"], "anzahl": len(liste), **z})


@router.post("/api/exo/modul")
async def api_exo_modul(user: str = Depends(_require_admin)):
    return JSONResponse(await asyncio.to_thread(exo_setup.modul_pruefen))


@router.post("/api/exo/verbindung")
async def api_exo_verbindung(user: str = Depends(_require_admin)):
    return JSONResponse(await asyncio.to_thread(exo_setup.verbindung_testen))


@router.post("/api/exo/connector")
async def api_exo_connector(request: Request, user: str = Depends(_require_admin)):
    daten = await request.json()
    hostname = (daten.get("hostname") or settings_store.get("PUBLIC_HOSTNAME") or "").strip()
    if not hostname:
        return JSONResponse({"ok": False, "output": "Hostname fehlt."}, status_code=400)
    ips = daten.get("sender_ips") or []
    if isinstance(ips, str):
        ips = [t.strip() for t in ips.split(",") if t.strip()]
    r = await asyncio.to_thread(exo_setup.connector_einrichten, hostname, ips)
    log.info("Inbound-Connector durch %s: %s", user, "ok" if r.get("ok") else "fehlgeschlagen")
    return JSONResponse(r)


@router.get("/api/exo/connector")
async def api_exo_connector_pruefen(user: str = Depends(_require_admin)):
    return JSONResponse(await asyncio.to_thread(exo_setup.connector_pruefen))


@router.post("/api/smarthost/test")
async def api_smarthost_test(user: str = Depends(_require_admin)):
    return JSONResponse(await asyncio.to_thread(smarthost.verbindungstest))


# ── Auth-Zertifikat (App-Registrierung) ──────────────────────────────────────

@router.post("/api/auth-cert/erzeugen")
async def api_auth_cert_erzeugen(user: str = Depends(_require_admin)):
    info = auth_cert.erzeugen()
    log.info("Auth-Zertifikat durch %s neu erzeugt", user)
    return JSONResponse({"ok": True, **info})


@router.post("/api/auth-cert/import")
async def api_auth_cert_import(user: str = Depends(_require_admin),
                               datei: UploadFile = File(...), passwort: str = Form("")):
    try:
        info = auth_cert.importieren(await datei.read(), passwort)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    log.info("Auth-Zertifikat durch %s importiert", user)
    return JSONResponse({"ok": True, **info})


@router.get("/api/auth-cert/public.cer")
async def api_auth_cert_public(user: str = Depends(_require_admin)):
    der = auth_cert.public_cer()
    if der is None:
        return JSONResponse({"ok": False, "error": "Kein Auth-Zertifikat vorhanden."}, status_code=404)
    return Response(der, media_type="application/pkix-cert",
                    headers={"Content-Disposition": 'attachment; filename="exo-smtp-relay-auth.cer"'})


# ── TLS-Zertifikat des Listeners ─────────────────────────────────────────────

@router.post("/api/tls/selbstsigniert")
async def api_tls_selbstsigniert(request: Request, user: str = Depends(_require_admin)):
    daten = await request.json()
    hostname = (daten.get("hostname") or settings_store.get("PUBLIC_HOSTNAME") or "").strip()
    info = tls_cert.selbstsigniert_erzeugen(hostname)
    log.info("TLS-Zertifikat durch %s neu erzeugt (%s) — wirkt nach Neustart", user, hostname)
    return JSONResponse({"ok": True, "neustart": True, **info})


@router.post("/api/tls/import")
async def api_tls_import(user: str = Depends(_require_admin), datei: UploadFile = File(...),
                         passwort: str = Form(""), hostname: str = Form(""),
                         uebergehen: str = Form("")):
    try:
        info = tls_cert.install_pfx(await datei.read(), passwort,
                                    expected_host=hostname or settings_store.get("PUBLIC_HOSTNAME") or "",
                                    allow_mismatch=uebergehen in ("1", "true", "on"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    log.info("TLS-Zertifikat durch %s importiert — wirkt nach Neustart", user)
    return JSONResponse({"ok": True, "neustart": True, **info})
