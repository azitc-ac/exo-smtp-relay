"""Aktualisierung aus fertigen Releases (seit 0.2.40) — Seite und API.

Die Arbeit liegt in `aktualisierung.py` (Prüfen, Laden, Auftrag) und
`update_tausch.py` (Austausch mit Rückfall). Alle Routen hängen an
`_require_admin`: Wer einspielt, entscheidet, welcher Code den Dienst
betreibt (geprüft von test_oberflaeche::test_jede_route_verlangt_anmeldung).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

import aktualisierung
import log_manager
import settings_store

from webui.deps import templates, log, _gateway_name, _require_admin

router = APIRouter()


@router.get("/aktualisierung", response_class=HTMLResponse)
async def aktualisierung_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="aktualisierung.html",
        context={"active": "aktualisierung", "gateway_name": _gateway_name(),
                 "s": settings_store.public_view(), "z": aktualisierung.zustand(),
                 "kanaele": aktualisierung.KANAELE,
                 "intervalle": aktualisierung.INTERVALLE_H,
                 "protokoll": str(log_manager.LOG_DIR / log_manager._LOG_FILENAME)})


@router.get("/api/aktualisierung")
async def api_aktualisierung(user: str = Depends(_require_admin)):
    return JSONResponse(await asyncio.to_thread(aktualisierung.zustand))


@router.post("/api/aktualisierung/pruefen")
async def api_aktualisierung_pruefen(user: str = Depends(_require_admin)):
    stand = await asyncio.to_thread(aktualisierung.pruefen)
    return JSONResponse(stand, status_code=200 if stand.get("ok") else 502)


@router.post("/api/aktualisierung/einspielen")
async def api_aktualisierung_einspielen(user: str = Depends(_require_admin)):
    r = await asyncio.to_thread(aktualisierung.einspielen, user)
    if not r.get("ok"):
        log.warning("Aktualisierung durch %s nicht angefordert: %s", user, r.get("error"))
    return JSONResponse(r, status_code=200 if r.get("ok") else 409)
