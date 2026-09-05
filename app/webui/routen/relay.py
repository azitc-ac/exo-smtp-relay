"""Schnittstelle des Relays — Geräteliste, Lernmodus, Abweisungen (Seite: Dashboard).

Inhaltlich das Routenmodul des Gateways; Abweichungen: kein `REINJECT_MODE`
(hier fest `smtp`), und der Zustand der Adressquelle wird mitgeliefert, weil
ein Relay ohne bekannte Adressen jede Einlieferung mit 451 abweist — das soll
man auf der Geräteseite sehen, nicht erst im Protokoll.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import exo_mailboxes
import relay_hosts
import settings_store
import smtp_relay

from webui.deps import log, _require_admin

router = APIRouter()


def _zustand() -> dict:
    bis = smtp_relay.lernmodus_bis()
    return {
        "aktiv": bis is not None,
        "bis": bis.strftime("%Y-%m-%dT%H:%M:%SZ") if bis else "",
        "rest_sek": max(0, int((bis - datetime.now(timezone.utc)).total_seconds())) if bis else 0,
        "bereiche": settings_store.get("SMTP_RELAY_LERN_NETZE") or [],
        "extern_vorgabe": bool(settings_store.get("SMTP_RELAY_EXTERN_VORGABE")),
        "max_minuten": smtp_relay.MAX_LERNDAUER_MIN,
        "standard_minuten": smtp_relay.STANDARD_LERNDAUER_MIN,
    }


@router.get("/api/relay/liste")
async def api_relay_liste(namen: int = 0, user: str = Depends(_require_admin)):
    if namen:
        relay_hosts.namen_nachtragen()
    return JSONResponse({
        "ok": True,
        "geraete": relay_hosts.liste(),
        "abgewiesen": relay_hosts.abgewiesene(),
        "fenster": list(relay_hosts.FENSTER),
        "lernmodus": _zustand(),
        "relay_an": bool(settings_store.get("SMTP_RELAY_ENABLED")),
        "adressen_bekannt": len(exo_mailboxes.known_addresses()),
        "smarthost": bool((settings_store.get("EXO_SMARTHOST") or "").strip()
                          or (settings_store.get("EXO_SUBMIT_MODE") == "submit")),
    })


@router.post("/api/relay/geraet")
async def api_relay_geraet(request: Request, user: str = Depends(_require_admin)):
    daten = await request.json()
    ip = (daten.get("ip") or "").strip()
    if not ip:
        return JSONResponse({"ok": False, "error": "Keine Adresse angegeben."}, status_code=400)
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return JSONResponse({"ok": False, "error": f"{ip!r} ist keine gültige IP-Adresse."},
                            status_code=400)
    felder = {k: daten[k] for k in ("dns", "kommentar", "ansprechpartner", "extern", "gesperrt")
              if k in daten}
    relay_hosts.speichern(ip, **felder)
    relay_hosts.vergiss_abweisung(ip)
    log.info("SMTP-Relay: Gerät %s durch %s gespeichert (%s)", ip, user,
             ", ".join(felder) or "nur angelegt")
    return JSONResponse({"ok": True, "geraet": relay_hosts.host(ip)})


@router.post("/api/relay/geraet/loeschen")
async def api_relay_geraet_loeschen(request: Request, user: str = Depends(_require_admin)):
    daten = await request.json()
    ip = (daten.get("ip") or "").strip()
    weg = relay_hosts.entfernen(ip)
    if weg:
        log.info("SMTP-Relay: Gerät %s durch %s entfernt", ip, user)
    return JSONResponse({"ok": weg})


@router.post("/api/relay/lernmodus")
async def api_relay_lernmodus(request: Request, user: str = Depends(_require_admin)):
    daten = await request.json()
    if not daten.get("start"):
        settings_store.update({"SMTP_RELAY_LERN_BIS": ""})
        log.info("SMTP-Relay: Lernmodus durch %s beendet", user)
        return JSONResponse({"ok": True, "lernmodus": _zustand()})
    bereiche = [str(b).strip() for b in (daten.get("bereiche") or []) if str(b).strip()]
    if not bereiche:
        return JSONResponse({"ok": False, "error": "Bereich erforderlich — bitte ein Netz "
                                                   "oder eine Spanne angeben."}, status_code=400)
    minuten = daten.get("minuten") or smtp_relay.STANDARD_LERNDAUER_MIN
    try:
        minuten = int(minuten)
    except (TypeError, ValueError):
        minuten = smtp_relay.STANDARD_LERNDAUER_MIN
    minuten = max(1, min(minuten, smtp_relay.MAX_LERNDAUER_MIN))
    settings_store.update({
        "SMTP_RELAY_LERN_NETZE": bereiche,
        "SMTP_RELAY_EXTERN_VORGABE": bool(daten.get("extern_vorgabe")),
        "SMTP_RELAY_LERN_BIS": (datetime.now(timezone.utc) + timedelta(minutes=minuten)).isoformat(),
    })
    zustand = _zustand()
    verstanden = len(smtp_relay._lernbereiche())
    log.info("SMTP-Relay: Lernmodus durch %s gestartet, %d Minuten, %d von %d Bereichen verstanden",
             user, minuten, verstanden, len(bereiche))
    return JSONResponse({"ok": True, "lernmodus": zustand,
                         "verstanden": verstanden, "eingetragen": len(bereiche)})


@router.post("/api/relay/abweisungen/leeren")
async def api_relay_abweisungen_leeren(user: str = Depends(_require_admin)):
    n = relay_hosts.abweisungen_leeren()
    log.info("SMTP-Relay: %d Abweisungen durch %s geleert", n, user)
    return JSONResponse({"ok": True, "geloescht": n})
