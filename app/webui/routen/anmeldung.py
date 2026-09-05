"""Anmeldung, Abmeldung, Passwort — nur örtlich.

Zwei Adressen sind ohne Wache erreichbar, weil vor der Anmeldung keine Sitzung
besteht: `/auth/login` (die Seite) und `/auth/local` (das Formular).
`tests/test_wachen.py` führt sie namentlich.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import config
import settings_store
import sitzung

from webui.deps import (templates, log, _gateway_name, _check_auth, _check_password,
                        _hash_password, _get_session_user, _password_change_required)

router = APIRouter()


def _cookie_secure() -> bool:
    return Path(config.SMTP_TLS_CERT).exists()


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "", next: str = "/"):
    if _get_session_user(request):
        return RedirectResponse(next or "/", status_code=302)
    return templates.TemplateResponse(
        request=request, name="login.html",
        context={"active": "login", "gateway_name": _gateway_name(), "error": error})


@router.post("/auth/local")
async def auth_local(request: Request):
    import login_drossel
    ip = request.client.host if request.client else "?"
    if login_drossel.gesperrt(ip):
        raise HTTPException(429, "Zu viele Fehlversuche — bitte kurz warten.")
    daten = await request.json()
    username = (daten.get("username") or "").strip()
    password = daten.get("password") or ""
    erwartet = settings_store.get("WEBUI_USERNAME") or "admin"
    if not (secrets.compare_digest(username.encode(), erwartet.encode()) and _check_password(password)):
        login_drossel.fehlversuch(ip)
        log.warning("Fehlgeschlagene Anmeldung von %s", ip)
        raise HTTPException(401, "Benutzername oder Passwort falsch")
    login_drossel.erfolg(ip)
    antwort = JSONResponse({"ok": True, "password_change_required": _password_change_required()})
    antwort.set_cookie(sitzung.SESSION_COOKIE, sitzung.create_session_cookie(username),
                       max_age=sitzung.SESSION_TTL, httponly=True, samesite="lax",
                       secure=_cookie_secure())
    log.info("Anmeldung: %s von %s", username, ip)
    return antwort


@router.get("/auth/logout")
async def auth_logout():
    antwort = RedirectResponse("/auth/login", status_code=302)
    antwort.delete_cookie(sitzung.SESSION_COOKIE)
    return antwort


@router.get("/api/whoami")
async def api_whoami(request: Request, user: str = Depends(_check_auth)):
    return JSONResponse({"upn": user, "role": "admin",
                         "password_change_required": _password_change_required()})


@router.post("/api/password")
async def api_password(request: Request, user: str = Depends(_check_auth)):
    daten = await request.json()
    alt = daten.get("current") or ""
    neu = daten.get("new") or ""
    if not _check_password(alt):
        return JSONResponse({"ok": False, "error": "Aktuelles Passwort falsch."}, status_code=400)
    if len(neu) < 10:
        return JSONResponse({"ok": False, "error": "Mindestens 10 Zeichen."}, status_code=400)
    settings_store.update({"ADMIN_PASSWORD_HASH": _hash_password(neu)})
    log.info("Passwort durch %s geändert", user)
    return JSONResponse({"ok": True})
