"""Testmail: eine echte Nachricht über DENSELBEN Weg wie Post von Geräten.

Vorbild ist die Testmail des Signatur-Gateways: Einlieferung per SMTP an den
eigenen Listener, nicht an der Annahme vorbei. Damit beweist ein „zugestellt“
den ganzen Weg — Annahme, Grenzen (`smtp_relay.pruefe`), Rückweg
(`smarthost.send`) und die Antwort von Exchange Online.

Zwei Besonderheiten gegenüber einem Gerät:

* **Einmal-Kennung statt Geräteliste.** Der Listener sieht die Testmail von
  127.0.0.1 (bzw. der Bind-Adresse). Diese Adresse in die Geräteliste zu
  schreiben, gäbe jedem Prozess auf dem Server das Relay frei. Stattdessen trägt
  die Testmail einen Kopf `X-Relay-Testmail` mit einer Kennung, die nur dieser
  Prozess kennt, einmal gilt und nach fünf Minuten verfällt.
* **Geprüft im Namen eines Geräts.** `pruefe()` (gespiegelt mit dem Gateway)
  lernt eine unbekannte Quelle als Gerät — mit 127.0.0.1 stünde danach der
  Server selbst in der Liste. Die Testmail wird deshalb mit der Adresse eines
  freigegebenen Geräts geprüft und unterliegt genau dessen Regeln (z. B.
  „extern erlaubt“). Das zeigt zugleich, ob dieses Gerät so senden dürfte.

Das Ergebnis bleibt in `TESTMAIL_LAUF` stehen (Anzeige in Einrichtung Schritt 6
und unter Einstellungen → Rückweg), mit Message-ID und der Antwort von Exchange.
"""
from __future__ import annotations

import email.utils
import logging
import secrets
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.parser import BytesHeaderParser

import config
import settings_store

log = logging.getLogger(__name__)

KOPF = "X-Relay-Testmail"
GUELTIG_S = 300
_offen: dict[str, dict] = {}


def _jetzt() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ziel_host() -> str:
    """Wohin die Testmail eingeliefert wird: die Bind-Adresse des Listeners,
    bei „alle Adressen“ die Loopback-Adresse."""
    bind = (config.SMTP_BIND or "").strip()
    return "127.0.0.1" if bind in ("", "0.0.0.0", "::") else bind


def eigene_quellen() -> set[str]:
    return {"127.0.0.1", "::1", ziel_host()}


def _aufraeumen() -> None:
    jetzt = time.monotonic()
    for k in [k for k, v in _offen.items() if v["bis"] < jetzt]:
        del _offen[k]


def einloesen(raw: bytes, peer_ip: str) -> dict | None:
    """Ist das eine Testmail dieses Prozesses? → ihr Auftrag (einmalig), sonst None.

    Gilt nur von einer eigenen Adresse des Servers und nur mit gültiger,
    unverbrauchter Kennung — alles andere läuft als gewöhnliche Einlieferung
    weiter (und wird ohne Geräteeintrag abgewiesen)."""
    if KOPF.encode() not in raw[:65536] or peer_ip not in eigene_quellen():
        return None
    try:
        kennung = (BytesHeaderParser().parsebytes(raw).get(KOPF) or "").strip()
    except Exception:                                         # noqa: BLE001
        return None
    _aufraeumen()
    return _offen.pop(kennung, None) if kennung else None


def merken(auftrag: dict, ok: bool, **felder) -> dict:
    lauf = {"zeit": _jetzt(), "ok": bool(ok), "von": auftrag.get("von", ""), "an": auftrag.get("an", ""),
            "geraet": auftrag.get("geraet", ""), "mid": auftrag.get("mid", ""), **felder}
    settings_store.update({"TESTMAIL_LAUF": lauf})
    (log.info if ok else log.warning)("Testmail %s → %s: %s", lauf["von"], lauf["an"],
                                      lauf.get("antwort") or lauf.get("fehler") or "")
    return lauf


def zustand() -> dict:
    """Für die Vorlage `_testmail.html`: letzter Lauf, Vorbelegung, wählbare Geräte."""
    import relay_hosts
    lauf = settings_store.get("TESTMAIL_LAUF") or {}
    geraete = [g for g in relay_hosts.liste() if not g.get("gesperrt")]
    return {"lauf": lauf, "geraete": geraete,
            "von": lauf.get("von") or (settings_store.get("SUBMIT_USER") or ""),
            "an": lauf.get("an") or ""}


def senden(von: str, an: str, geraet: str) -> dict:
    """Testmail einliefern und das Ergebnis liefern (auch gespeichert in TESTMAIL_LAUF)."""
    import relay_hosts
    von, an, geraet = (von or "").strip(), (an or "").strip(), (geraet or "").strip()
    if "@" not in von or "@" not in an:
        return {"ok": False, "fehler": "Absender und Empfänger als E-Mail-Adresse angeben."}
    eintrag = relay_hosts.host(geraet) if geraet else None
    if not eintrag or eintrag.get("gesperrt"):
        return {"ok": False, "fehler": "Ein freigegebenes Gerät wählen — die Testmail wird mit "
                                       "dessen Regeln geprüft (Einrichtung Schritt 5)."}
    domain = (settings_store.get("PUBLIC_HOSTNAME") or "exo-smtp-relay").strip()
    mid = email.utils.make_msgid(domain=domain)
    kennung = secrets.token_urlsafe(24)
    auftrag = {"von": von, "an": an, "geraet": geraet, "mid": mid, "bis": time.monotonic() + GUELTIG_S}
    _aufraeumen()
    _offen[kennung] = auftrag

    msg = EmailMessage()
    msg["From"], msg["To"] = von, an
    msg["Subject"] = "Testmail vom EXO SMTP Relay"
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = mid
    msg[KOPF] = kennung
    msg.set_content(
        "Diese Nachricht hat das EXO SMTP Relay über denselben Weg verschickt wie Post von "
        f"Geräten: Annahme, Prüfung mit den Regeln des Geräts {geraet}, Rückweg zu Exchange "
        "Online.\n\nKommt sie an, funktioniert der Weg.\n")

    # Ohne Zertifikatsprüfung, bewusst (wie im Gateway): Ziel ist der eigene Listener,
    # dessen Zertifikat auf den öffentlichen Namen lautet, nicht auf die Adresse hier.
    import tls_vertrauen
    ctx = tls_vertrauen.client_kontext()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with smtplib.SMTP(ziel_host(), config.SMTP_PORT, timeout=120) as smtp:
            smtp.ehlo()
            if smtp.has_extn("starttls"):
                smtp.starttls(context=ctx)
                smtp.ehlo()
            smtp.send_message(msg)
    except smtplib.SMTPResponseException as exc:
        # Abgewiesen: Hat der Handler die Kennung eingelöst, steht sein Ergebnis schon
        # in TESTMAIL_LAUF (Grund, Antwort). Sonst kam die Mail gar nicht bis dahin.
        _offen.pop(kennung, None)
        lauf = settings_store.get("TESTMAIL_LAUF") or {}
        if lauf.get("mid") == mid:
            return lauf
        text = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        return merken(auftrag, False, fehler=f"Listener: {exc.smtp_code} {text}")
    except Exception as exc:                                  # noqa: BLE001
        _offen.pop(kennung, None)
        return merken(auftrag, False, fehler=f"Listener auf {ziel_host()}:{config.SMTP_PORT} "
                                             f"nicht erreichbar: {exc}")
    lauf = settings_store.get("TESTMAIL_LAUF") or {}
    if lauf.get("mid") != mid:
        # Angenommen, aber nicht als Testmail erkannt — darf nicht vorkommen; ehrlich melden.
        return merken(auftrag, False, fehler="Der Listener hat angenommen, die Testmail aber nicht "
                                             "als solche erkannt — Ergebnis unbekannt.")
    return lauf
