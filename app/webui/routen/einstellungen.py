"""Einstellungen: Rückweg, Tenant, Adressquelle, Zertifikate, Connector.

Alle Schreibwege gehen über `settings_store.nur_bekannte()`: Unbekanntes und
Festes (`REINJECT_MODE`) wird gemeldet, nicht still geschluckt.
"""
from __future__ import annotations

import asyncio
import sys

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

import auth_cert
import config
import exo_mailboxes
import exo_setup
import settings_store
import smarthost
import testmail
import le_certbot
import tls_acme_dns
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
                 "le": _le_zustand(),
                 "exo": exo_mailboxes.zustand(),
                 "pwsh": config.PWSH, "data_dir": config.DATA_DIR,
                 "smtp_port": config.SMTP_PORT, "tm": testmail.zustand()})


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
    import aktualisierung
    if "UPDATE_KANAL" in uebernommen and uebernommen["UPDATE_KANAL"] not in aktualisierung.KANAELE:
        return JSONResponse({"ok": False, "error": "Kanal muss stable oder beta sein."}, status_code=400)
    if "UPDATE_PRUEF_STUNDEN" in uebernommen and \
            str(uebernommen["UPDATE_PRUEF_STUNDEN"]) not in {str(h) for h in aktualisierung.INTERVALLE_H}:
        return JSONResponse({"ok": False, "error": "Unzulässiges Prüfintervall."}, status_code=400)
    if "LE_METHODE" in uebernommen and uebernommen["LE_METHODE"] not in ("http01", "dns01"):
        return JSONResponse({"ok": False, "error": "Methode muss http01 oder dns01 sein."}, status_code=400)
    if "DNS01_PROVIDER" in uebernommen:
        import dns_provider
        if (uebernommen["DNS01_PROVIDER"] or "") not in ("", *dns_provider.PROVIDER):
            return JSONResponse({"ok": False, "error": "Unbekannter DNS-Provider."}, status_code=400)
    settings_store.update(uebernommen)
    if "ADRESSEN_ZUSAETZLICH" in uebernommen:
        exo_mailboxes.invalidate()
    if "LOG_LEVEL" in uebernommen:
        import log_manager
        log_manager.stufe_anwenden()    # wirkt sofort, nicht erst nach dem Neustart
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


@router.post("/api/testmail")
async def api_testmail(request: Request, user: str = Depends(_require_admin)):
    """Testmail über den eigenen Listener (testmail.py) — derselbe Weg wie Gerätepost."""
    import testmail
    daten = await request.json()
    log.info("Testmail durch %s: %s → %s (Regeln von %s)", user, daten.get("von"), daten.get("an"),
             daten.get("geraet"))
    erg = await asyncio.to_thread(testmail.senden, daten.get("von") or "", daten.get("an") or "",
                                  daten.get("geraet") or "")
    return JSONResponse(erg, status_code=200 if erg.get("ok") or erg.get("zeit") else 400)


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
    log.info("TLS-Zertifikat durch %s neu erzeugt (%s) — SMTP sofort, Weboberfläche nach Neustart", user, hostname)
    return JSONResponse({"ok": True, "neustart_webui": True, **info})


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
    log.info("TLS-Zertifikat durch %s importiert — SMTP sofort, Weboberfläche nach Neustart", user)
    return JSONResponse({"ok": True, "neustart_webui": True, **info})


# ── Let's Encrypt (seit 0.2.38, wie Gateway) ─────────────────────────────────
# HTTP-01 über certbot (le_certbot) und DNS-01 von Hand (tls_acme_dns). Beide
# schreiben über tls_cert._schreiben; der Listener lädt das Zertifikat bei der
# nächsten Verbindung, der Rückweg zu Exchange beim nächsten Versand.

def _katalog() -> list[dict]:
    """Provider-Katalog für die Oberfläche: je Provider die Felder mit ihrem
    Einstellungsnamen (dieselbe Bildung wie settings_store und tls_acme_dns)."""
    import dns_provider
    return [{"name": n, "titel": cls.TITEL, "vorbereitet": cls.VORBEREITET,
             "felder": [{"id": tls_acme_dns.feld_schluessel(n, f.schluessel), "titel": f.titel,
                         "geheim": f.geheim, "pflicht": f.pflicht, "hinweis": f.hinweis,
                         "auswahl": list(f.auswahl)} for f in cls.FELDER]}
            for n, cls in dns_provider.PROVIDER.items()]


def _le_zustand() -> dict:
    import dns_provider
    p = tls_acme_dns.pending() or {}
    return {"certbot": le_certbot.verfuegbar(), "verwaltet": le_certbot.verwaltet(),
            "windows": sys.platform == "win32",
            "methode": settings_store.get("LE_METHODE") or "http01",
            "provider": settings_store.get("DNS01_PROVIDER") or "",
            "katalog": _katalog(), "katalog_version": dns_provider.__version__,
            "lauf": settings_store.get("LE_LAUF") or {},
            "dns01": {k: p.get(k) for k in ("domain", "record_name", "record_value", "staging")}
            if p else None}


def _le_angaben(daten: dict) -> tuple[str, str]:
    domain = (daten.get("domain") or settings_store.get("LE_DOMAIN")
              or settings_store.get("PUBLIC_HOSTNAME") or "").strip().lower()
    email = (daten.get("email") or settings_store.get("LE_EMAIL") or "").strip()
    return domain, email


@router.post("/api/letsencrypt")
async def api_letsencrypt(request: Request, user: str = Depends(_require_admin)):
    """HTTP-01 über certbot — derselbe Weg wie die automatische Erneuerung."""
    domain, email = _le_angaben(await request.json())
    if not domain or not email:
        return JSONResponse({"ok": False, "error": "Domain und E-Mail sind erforderlich."},
                            status_code=400)
    try:
        namen = tls_acme_dns.namen_aus(domain)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if any(n.startswith("*.") for n in namen) or len(namen) != 1:
        return JSONResponse({"ok": False, "error": "HTTP-01 nur für genau einen Namen ohne Wildcard — "
                                                   "für *.… oder mehrere Namen DNS-01 nehmen."}, status_code=400)
    settings_store.update({"LE_DOMAIN": domain, "LE_EMAIL": email, "LE_METHODE": "http01"})
    ok, info = await asyncio.to_thread(le_certbot.ausstellen, domain, email)
    le_certbot.lauf_vermerken(ok, f"Ausgestellt, gültig bis {info}" if ok else info, "http01")
    if not ok:
        log.error("certbot für %s fehlgeschlagen: %s", domain, info)
        return JSONResponse({"ok": False, "error": info}, status_code=502)
    log.info("Let's-Encrypt-Zertifikat für %s übernommen (gültig bis %s) durch %s", domain, info, user)
    return JSONResponse({"ok": True, "neustart_webui": True, **tls_cert.info()})


@router.post("/api/tls/dns01/start")
async def api_dns01_start(request: Request, user: str = Depends(_require_admin)):
    daten = await request.json()
    domain, email = _le_angaben(daten)
    if not domain:
        return JSONResponse({"ok": False, "error": "Domain fehlt."}, status_code=400)
    try:
        namen = tls_acme_dns.namen_aus(domain)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if len(namen) != 1 or namen[0].startswith("*."):
        return JSONResponse({"ok": False, "error": "DNS-01 von Hand nur für genau einen Namen ohne Wildcard — "
                                                   "für *.… oder mehrere Namen einen DNS-Provider wählen."},
                            status_code=400)
    settings_store.update({"LE_DOMAIN": domain, "LE_EMAIL": email, "LE_METHODE": "dns01"})
    try:
        rec = await tls_acme_dns.start(domain, email, bool(daten.get("staging")))
    except Exception as exc:                                  # noqa: BLE001
        log.error("DNS-01 für %s nicht gestartet: %s", domain, exc)
        return JSONResponse({"ok": False, "error": f"DNS-01 konnte nicht gestartet werden: {exc}"},
                            status_code=502)
    log.info("DNS-01-Bestellung für %s durch %s gestartet", domain, user)
    return JSONResponse({"ok": True, **rec})


@router.post("/api/tls/dns01/finish")
async def api_dns01_finish(user: str = Depends(_require_admin)):
    try:
        info = await tls_acme_dns.finish()
    except Exception as exc:                                  # noqa: BLE001
        le_certbot.lauf_vermerken(False, f"DNS-01: {exc}", "dns01")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    le_certbot.lauf_vermerken(True, "DNS-01: ausgestellt, gültig bis "
                              + (info.get("not_after") or "?")[:10]
                              + (" (Testumgebung)" if info.get("staging") else ""), "dns01")
    log.info("Let's-Encrypt-Zertifikat (DNS-01) für %s durch %s übernommen",
             ", ".join(info.get("hostnames") or []), user)
    return JSONResponse({"ok": True, "neustart_webui": True, **tls_cert.info()})


@router.post("/api/tls/dns01/verwerfen")
async def api_dns01_verwerfen(user: str = Depends(_require_admin)):
    tls_acme_dns.verwerfen()
    log.info("Offene DNS-01-Bestellung durch %s verworfen", user)
    return JSONResponse({"ok": True})


# ── DNS-01 über eine DNS-API (seit 0.2.57-beta) ──────────────────────────────
# Ausstellen hier und Erneuern im Zeitplaner gehen durch tls_acme_dns.automatisch
# — mit den GESPEICHERTEN Einstellungen (die Oberfläche speichert vorher).

@router.post("/api/tls/dns01/test")
async def api_dns01_test(request: Request, user: str = Depends(_require_admin)):
    """Nur lesend: Zonen auflisten. Nimmt die Formularwerte (ungespeichert);
    ein maskiertes Geheimnis steht für das gespeicherte. Antwort ohne Geheimnis."""
    import dns_provider
    daten = await request.json()
    name = (daten.get("provider") or "").strip().lower()
    try:
        p = tls_acme_dns.provider_aus_einstellungen(name, daten.get("werte") or {})
        zonen = await asyncio.to_thread(p.zonen)
    except dns_provider.DnsProviderFehler as exc:
        log.warning("DNS-Provider-Test (%s) durch %s: %s", name or "-", user, exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    log.info("DNS-Provider-Test (%s) durch %s: %d Zone(n)", name, user, len(zonen))
    return JSONResponse({"ok": True, "provider": name, "zonen": zonen})


@router.post("/api/tls/dns01/auto")
async def api_dns01_auto(request: Request, user: str = Depends(_require_admin)):
    daten = await request.json()
    domain, email = _le_angaben(daten)
    try:
        namen = tls_acme_dns.namen_aus(domain)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if not namen:
        return JSONResponse({"ok": False, "error": "Domain fehlt."}, status_code=400)
    provider = settings_store.get("DNS01_PROVIDER") or ""
    if not provider:
        return JSONResponse({"ok": False, "error": "Kein DNS-Provider gewählt und gespeichert."}, status_code=400)
    settings_store.update({"LE_DOMAIN": domain, "LE_EMAIL": email, "LE_METHODE": "dns01"})
    staging = bool(daten.get("staging"))
    try:
        info = await tls_acme_dns.automatisch(namen, email, staging)
    except Exception as exc:                                  # noqa: BLE001
        le_certbot.lauf_vermerken(False, f"DNS-01 ({provider}): {exc}", "dns01")
        log.error("DNS-01 (%s) für %s fehlgeschlagen: %s", provider, ", ".join(namen), exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    le_certbot.lauf_vermerken(True, f"DNS-01 ({provider}): ausgestellt, gültig bis "
                              + (info.get("not_after") or "?")[:10]
                              + (" (Testumgebung)" if staging else ""), "dns01")
    log.info("Let's-Encrypt-Zertifikat (DNS-01, %s) für %s durch %s übernommen",
             provider, ", ".join(namen), user)
    return JSONResponse({"ok": True, "neustart_webui": True, **tls_cert.info()})
