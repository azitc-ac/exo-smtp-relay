"""Die Weboberfläche — Dashboard, Einrichtung, Einstellungen, Protokoll.

Fünf Routenmodule, ein Fundament (`deps.py`). Die Schnittstellenbeschreibung
(/docs, /openapi.json) ist abgeschaltet: Für einen Dienst, der mit Port 25 im
Netz steht, ist die Landkarte der eigenen Angriffsfläche keine Beigabe.
"""
from __future__ import annotations

import logging
import urllib.parse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import settings_store

from webui.deps import _NotAuthenticated, _STATIC_DIR, log  # noqa: F401

app = FastAPI(title="EXO SMTP Relay", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

from webui.routen import anmeldung as _routen_anmeldung        # noqa: E402
from webui.routen import betrieb as _routen_betrieb            # noqa: E402
from webui.routen import einrichtung as _routen_einrichtung    # noqa: E402
from webui.routen import einstellungen as _routen_einstellungen  # noqa: E402
from webui.routen import relay as _routen_relay                # noqa: E402

# EINE Quelle: hieraus werden die Router eingebunden, und die Tests zählen
# daraus die Routen ab (include_router kopiert ab FastAPI 0.139 nicht mehr
# nach app.routes).
ROUTENMODULE = (_routen_anmeldung, _routen_betrieb, _routen_einrichtung,
                _routen_einstellungen, _routen_relay)
for _modul in ROUTENMODULE:
    app.include_router(_modul.router)


@app.exception_handler(_NotAuthenticated)
async def _not_authenticated_handler(request: Request, exc: _NotAuthenticated):
    if exc.is_api:
        return JSONResponse({"detail": "Nicht angemeldet"}, status_code=401)
    next_url = urllib.parse.quote(str(request.url.path), safe="")
    return RedirectResponse(f"/auth/login?next={next_url}", status_code=302)


@app.middleware("http")
async def _kein_cache_fuer_html(request: Request, call_next):
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def _sicherheits_header(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
    return response


# ── Herkunftsprüfung gegen CSRF ─────────────────────────────────────────────
_SICHERE_METHODEN = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_HERKUNFT_FREI = ("/auth/",)


def _erlaubte_hosts(request: Request) -> set[str]:
    hosts = {"localhost", "127.0.0.1"}
    for h in (request.headers.get("host"), request.headers.get("x-forwarded-host")):
        if h:
            hosts.add(h.split(",")[0].split(":")[0].strip().lower())
    ph = (settings_store.get("PUBLIC_HOSTNAME") or "").strip().lower()
    if ph:
        hosts.add(ph.split(":")[0])
    return hosts


@app.middleware("http")
async def _herkunft_pruefen(request: Request, call_next):
    if request.method not in _SICHERE_METHODEN and not request.url.path.startswith(_HERKUNFT_FREI):
        quelle = request.headers.get("origin") or request.headers.get("referer") or ""
        if quelle:
            q_host = (urllib.parse.urlparse(quelle).hostname or "").lower()
            if q_host and q_host not in _erlaubte_hosts(request):
                log.warning("Herkunftsprüfung: %s %s von fremder Herkunft %r abgewiesen",
                            request.method, request.url.path, quelle)
                return JSONResponse({"detail": "Ungültige Herkunft"}, status_code=403)
    return await call_next(request)
