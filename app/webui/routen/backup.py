"""Routen der Sicherung — Herunterladen, Ansehen, Wiederherstellen (v0.2.35).

Übernommen aus dem Gateway (routen/backup.py, Stand f0c95af), gleiche Pfade.
Die Arbeit liegt in `backup_manager`; diese Routen nehmen die Datei entgegen,
prüfen die Auswahl und reichen weiter.

⚠️ ALLE HÄNGEN AN `_require_admin`, und das ist hier keine Formalie:
`/api/backup/download` gibt den gesamten Datenbestand heraus — Einstellungen
mit Zugangsdaten, private Schlüssel, die Geräteliste. `/api/backup/restore`
schreibt ihn zurück und entscheidet damit, wer Post einliefern darf.
Geprüft von `tests/test_oberflaeche.py::test_jede_route_verlangt_anmeldung`.

Die Arbeitsteilung zwischen `inspect` und `restore` ist Absicht: Ein Aufruf,
der nichts verändert, heisst nicht wie einer, der es tut.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

import config

from webui.deps import templates, log, _gateway_name, _require_admin

router = APIRouter()


@router.get("/backup", response_class=HTMLResponse)
async def backup_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="backup.html",
        context={"active": "backup", "gateway_name": _gateway_name(),
                 "data_dir": config.DATA_DIR,
                 "tls_cert": config.SMTP_TLS_CERT, "tls_key": config.SMTP_TLS_KEY})


# Verschlüsselte Backups: Das Passwort gehört nicht in eine URL (Protokolle,
# Verlauf). Deshalb erst POST mit Passwort → fertige Datei liegt kurz unter
# einer Einmal-Kennung, dann GET mit dieser Kennung (der gemeinsame
# Download-Baustein `dateiHerunterladen` kann nur GET). uvicorn läuft mit
# einem Prozess, der Speicher genügt.
_ABRUFE: dict[str, tuple[float, bytes, str]] = {}
_ABRUF_TTL_S = 300


@router.post("/api/backup/erstellen")
async def api_backup_erstellen(request: Request, user: str = Depends(_require_admin)):
    """Verschlüsseltes Backup erstellen; liefert eine Einmal-Kennung zum Abholen."""
    import secrets
    import time
    import backup_manager as _bm
    try:
        daten = await request.json()
    except Exception:                                         # noqa: BLE001
        daten = {}
    passwort = str(daten.get("passwort") or "")
    if len(passwort) < _bm.MIN_PASSWORT:
        raise HTTPException(400, f"Passwort zu kurz (mindestens {_bm.MIN_PASSWORT} Zeichen).")
    inhalt, filename = await asyncio.get_event_loop().run_in_executor(
        None, _bm.create_backup, passwort)
    jetzt = time.monotonic()
    for k in [k for k, (t, _, _) in _ABRUFE.items() if jetzt - t > _ABRUF_TTL_S]:
        _ABRUFE.pop(k, None)
    abruf = secrets.token_urlsafe(24)
    _ABRUFE[abruf] = (jetzt, inhalt, filename)
    log.info("Verschlüsseltes Backup durch %s erstellt", user)
    return JSONResponse({"ok": True, "abruf": abruf, "name": filename})


@router.get("/api/backup/download")
async def api_backup_download(abruf: str = "", user: str = Depends(_require_admin)):
    """Backup herunterladen — ohne `abruf` ein neues, unverschlüsseltes ZIP;
    mit `abruf` die zuvor verschlüsselt erstellte Datei (genau einmal)."""
    import time
    import backup_manager as _bm
    if abruf:
        eintrag = _ABRUFE.pop(abruf, None)
        if eintrag is None or time.monotonic() - eintrag[0] > _ABRUF_TTL_S:
            raise HTTPException(404, "Abruf abgelaufen — Backup bitte neu erstellen.")
        _, inhalt, filename = eintrag
        art = "application/octet-stream"
    else:
        inhalt, filename = await asyncio.get_event_loop().run_in_executor(
            None, _bm.create_backup)
        art = "application/zip"
        log.warning("UNVERSCHLÜSSELTES Backup durch %s heruntergeladen", user)
    return Response(content=inhalt, media_type=art,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _oeffnen(data: bytes, passwort: str) -> bytes | dict:
    """Hochgeladene Datei entschlüsseln — oder die Fehlerantwort liefern."""
    import backup_manager as _bm
    try:
        return _bm.oeffnen(data, passwort)
    except _bm.PasswortFehler as exc:
        return {"ok": False, "error": str(exc), "passwort_noetig": True,
                "gruppen": [], "restored_files": 0, "warnings": []}


@router.post("/api/backup/inspect")
async def api_backup_inspect(file: UploadFile = File(...), passwort: str = Form(""),
                             _user: str = Depends(_require_admin)):
    """Inhalt eines Backups anzeigen, OHNE etwas zu schreiben."""
    import backup_manager as _bm
    data = await asyncio.get_event_loop().run_in_executor(
        None, _oeffnen, await file.read(), passwort)
    if isinstance(data, dict):
        return JSONResponse(data)
    ergebnis = await asyncio.get_event_loop().run_in_executor(None, _bm.inspect_backup, data)
    return JSONResponse(ergebnis)


@router.post("/api/backup/restore")
async def api_backup_restore(file: UploadFile = File(...), auswahl: str = Form(""),
                             passwort: str = Form(""), user: str = Depends(_require_admin)):
    """Backup hochladen und wiederherstellen.

    `auswahl` ist eine JSON-Liste von Dateinamen aus dem ZIP. Fehlt sie, wird
    ALLES wiederhergestellt."""
    import json as _json
    import backup_manager as _bm
    data = await asyncio.get_event_loop().run_in_executor(
        None, _oeffnen, await file.read(), passwort)
    if isinstance(data, dict):
        return JSONResponse(data)

    gewaehlt = None
    if auswahl.strip():
        try:
            gewaehlt = _json.loads(auswahl)
        except Exception:                                     # noqa: BLE001
            raise HTTPException(400, "Auswahl ist kein gültiges JSON")
        if not isinstance(gewaehlt, list):
            raise HTTPException(400, "Auswahl muss eine Liste von Dateinamen sein")

    log.warning("Backup-Wiederherstellung durch %s gestartet", user)
    result = await asyncio.get_event_loop().run_in_executor(
        None, _bm.restore_backup, data, gewaehlt)
    return JSONResponse(result)
