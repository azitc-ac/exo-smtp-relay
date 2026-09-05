"""Betrieb: Erreichbarkeit, Übersicht, Protokoll, Mail-Protokoll."""
from __future__ import annotations

import asyncio
import json as _json
import queue as _queue_mod

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

import config
import exo_mailboxes
import mail_audit
import relay_hosts
import settings_store
import smtp_relay
import tls_cert

from webui.deps import (templates, _gateway_name, _require_admin, _make_log_token,
                        _check_log_token, _LOG_BUFFER, _LOG_SUBSCRIBERS, _LOG_SUBSCRIBERS_LOCK)

router = APIRouter()


@router.get("/health")
async def health():
    """Ohne Anmeldung — für Docker-HEALTHCHECK und den Online-Punkt der Leiste.
    Sagt, ob der Listener bedient; verrät sonst nichts."""
    import runtime_state
    c = runtime_state.smtp_controller
    smtp_ok = bool(c and getattr(c, "server", None))
    return JSONResponse({"ok": True, "smtp": smtp_ok, "version": config.VERSION},
                        status_code=200 if smtp_ok else 503)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(_require_admin)):
    # Bis der Assistent abgeschlossen ist, führt der Weg dorthin — wie beim Gateway.
    if not settings_store.get("SETUP_COMPLETE"):
        return RedirectResponse("/einrichtung", status_code=302)
    return templates.TemplateResponse(
        request=request, name="dashboard.html",
        context={"active": "dashboard", "gateway_name": _gateway_name(),
                 "fenster": list(relay_hosts.FENSTER)})


@router.get("/api/dashboard")
async def api_dashboard(tage: int = 30, namen: int = 0, user: str = Depends(_require_admin)):
    """Alles fürs Dashboard in einem Aufruf: Zustand, Zähler, Geräte mit
    Auswertung (TLS/Klartext, intern/extern), Abweisungen, Lernmodus."""
    tage = tage if tage in (1, 7, 30, 90) else 30
    if namen:
        relay_hosts.namen_nachtragen()
    geraete = relay_hosts.liste()
    auswertung = mail_audit.auswertung(tage)
    for g in geraete:
        g["statistik"] = auswertung["quellen"].get(g["ip"]) or {
            "zugestellt": 0, "tls": 0, "klartext": 0, "intern": 0, "extern": 0, "abgelehnt": 0, "fehler": 0}
    adressen = exo_mailboxes.known_addresses()
    modus = settings_store.get("EXO_SUBMIT_MODE") or "smarthost"
    rueckweg_ok = bool((settings_store.get("EXO_SMARTHOST") or "").strip()) if modus == "smarthost" \
        else bool(settings_store.get("SUBMIT_USER") and settings_store.get("SUBMIT_PASSWORD"))
    bis = smtp_relay.lernmodus_bis()
    from datetime import datetime, timezone
    return JSONResponse({
        "ok": True,
        "tage": tage,
        "relay_an": bool(settings_store.get("SMTP_RELAY_ENABLED")),
        "lernmodus": {
            "aktiv": bis is not None,
            "rest_sek": max(0, int((bis - datetime.now(timezone.utc)).total_seconds())) if bis else 0,
            "bereiche": settings_store.get("SMTP_RELAY_LERN_NETZE") or [],
            "extern_vorgabe": bool(settings_store.get("SMTP_RELAY_EXTERN_VORGABE")),
            "max_minuten": smtp_relay.MAX_LERNDAUER_MIN,
            "standard_minuten": smtp_relay.STANDARD_LERNDAUER_MIN,
        },
        "geraete": geraete,
        "fenster": list(relay_hosts.FENSTER),
        "abgewiesen": relay_hosts.abgewiesene(),
        "gesamt": auswertung["gesamt"],
        "heute": mail_audit.zaehler_heute(),
        "adressen": len(adressen),
        "exo": exo_mailboxes.zustand(),
        "rueckweg": {"modus": modus, "konfiguriert": rueckweg_ok,
                     "ziel": settings_store.get("EXO_SMARTHOST") if modus == "smarthost"
                     else settings_store.get("SUBMIT_HOST")},
        "tls": tls_cert.info(),
        "ereignisse": mail_audit.query_events(limit=25),
        "smtp_port": config.SMTP_PORT,
    })


@router.get("/log", response_class=HTMLResponse)
async def log_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="log.html",
        context={"active": "log", "stream_token": _make_log_token(),
                 "gateway_name": _gateway_name()})


@router.get("/log/stream")
async def log_stream(request: Request, token: str = ""):
    if not _check_log_token(token):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    q: _queue_mod.Queue = _queue_mod.Queue(maxsize=200)
    with _LOG_SUBSCRIBERS_LOCK:
        _LOG_SUBSCRIBERS.append(q)

    async def generate():
        for line in list(_LOG_BUFFER):
            yield f"data: {_json.dumps(line)}\n\n"
        try:
            while True:
                try:
                    line = q.get_nowait()
                    yield f"data: {_json.dumps(line)}\n\n"
                except _queue_mod.Empty:
                    await asyncio.sleep(0.4)
                    yield ": keepalive\n\n"
        finally:
            with _LOG_SUBSCRIBERS_LOCK:
                try:
                    _LOG_SUBSCRIBERS.remove(q)
                except ValueError:
                    pass

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/logs/search")
async def api_logs_search(q: str = "", time_from: str = "", time_to: str = "",
                          user: str = Depends(_require_admin)):
    if not q and not (time_from or time_to):
        raise HTTPException(400, "Suchbegriff oder Zeitraum fehlt")
    import log_manager
    results = log_manager.search(q, max_lines=500, time_from=time_from, time_to=time_to)
    return JSONResponse({"results": results, "count": len(results)})


@router.get("/api/audit/events")
async def api_audit_events(action: str | None = None, quelle: str | None = None,
                           limit: int = 200, offset: int = 0,
                           user: str = Depends(_require_admin)):
    return JSONResponse({"events": mail_audit.query_events(action=action, quelle=quelle,
                                                           limit=min(limit, 500), offset=offset)})
