"""Anmeldung, Abmeldung, Passwort — nur örtlich.

Zwei Adressen sind ohne Wache erreichbar, weil vor der Anmeldung keine Sitzung
besteht: `/auth/login` (die Seite) und `/auth/local` (das Formular).
`tests/test_wachen.py` führt sie namentlich.
"""
from __future__ import annotations

import re
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


_ZIEL_RE = re.compile(r"^/(?![/\\])[^\s\\\x00-\x1f\x7f]*$")


def _sicheres_ziel(ziel) -> str:
    """Wohin nach der Anmeldung weitergeleitet werden darf: nur ein Pfad auf
    DIESEM Server, sonst "/".

    `next` kommt aus der Adresszeile und ist damit frei wählbar. Ohne Prüfung
    führt ein präparierter Anmeldelink nach dem Login woandershin — auf eine
    fremde Seite (`//fremd.example`, `/\\fremd.example`: Browser lesen beides
    als Host) oder, wo das Ziel im Browser per `location.href` gesetzt wird, in
    ein `javascript:`-Ziel mit der frischen Sitzung.

    EINE Prüfung für alle Wege (wie im Gateway seit v1.9.138): die
    Weiterleitung einer schon angemeldeten Sitzung und das Ziel, das die
    Anmeldeseite nach dem Formular ansteuert.
    """
    if not isinstance(ziel, str) or not _ZIEL_RE.match(ziel):
        return "/"
    return ziel


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "", next: str = "/"):
    if _get_session_user(request):
        return RedirectResponse(_sicheres_ziel(next), status_code=302)
    return templates.TemplateResponse(
        request=request, name="login.html",
        context={"active": "login", "gateway_name": _gateway_name(), "error": error,
                 "next": _sicheres_ziel(next)})


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
    antwort = JSONResponse({"ok": True})
    # Der Wechsel beendet alle Sitzungen (sitzung._kennwort_kennung) — auch die
    # eigene. Wer gerade mit Sitzung angemeldet ist, bekommt deshalb sofort ein
    # neues Merkmal; alle anderen müssen sich neu anmelden.
    if request.cookies.get(sitzung.SESSION_COOKIE):
        antwort.set_cookie(sitzung.SESSION_COOKIE, sitzung.create_session_cookie(user),
                           max_age=sitzung.SESSION_TTL, httponly=True, samesite="lax",
                           secure=_cookie_secure())
    return antwort
