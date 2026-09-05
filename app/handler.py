"""Der Mailpfad: annehmen, prüfen, weiterreichen — nichts sonst.

Im grossen Gateway ist der Relay-Zweig eine Abzweigung in `handle_DATA`, hinter
der die Signatur- und S/MIME-Verarbeitung liegt. Hier ist er der ganze Weg:

    Quelle in der Geräteliste (oder im Lernlauf)?   → sonst 554
    Absenderdomäne eigen, Ziel zulässig?            → sonst 550 / 451
    An Exchange übergeben                            → sonst 451
    Zählen, protokollieren                          → 250

Die Regeln selbst stehen in `smtp_relay.py` und sind mit dem Gateway
inhaltsgleich (tools/driftcheck.py). Diese Datei ist nur die Verdrahtung —
und trägt die gefährlichere Invariante: Es gibt KEINEN zweiten Weg an der
Geräteliste vorbei. Kein Exchange-Adressbereich, keine Zusatzliste.

⚠️ GEZÄHLT WIRD ZUGESTELLTE POST. Das Gateway zählt vor der Zustellung; hier
danach. Ein Gerät, dessen Post am Smarthost scheitert, soll in der Übersicht
nicht als „rege" erscheinen — es kommt ja nichts an.
"""
from __future__ import annotations

import asyncio
import email
import email.header
import email.utils
import logging
import time

import mail_audit
import mail_trace
import relay_hosts
import settings_store
import smarthost
import smtp_relay

log = logging.getLogger(__name__)


def _decode_subject(raw: str) -> str:
    try:
        teile = email.header.decode_header(raw or "")
        return "".join(t.decode(enc or "utf-8", errors="replace") if isinstance(t, bytes) else t
                       for t, enc in teile)
    except Exception:                                         # noqa: BLE001
        return raw or ""


def _received_header(peer_ip: str, tls: bool) -> bytes:
    """Eine `Received:`-Zeile, wie sie jedes Relay setzt — damit im Kopf der
    Nachricht steht, von welchem Gerät sie kam. Exchange hängt seine eigenen
    darüber; diese hier ist die einzige, die das Gerät nennt."""
    eigen = (settings_store.get("PUBLIC_HOSTNAME") or "exo-smtp-relay").strip()
    proto = "ESMTPS" if tls else "ESMTP"
    zeile = (f"Received: from [{peer_ip}] by {eigen} (EXO SMTP Relay) with {proto}; "
             f"{email.utils.format_datetime(email.utils.localtime())}\r\n")
    return zeile.encode("ascii", errors="replace")


class RelayHandler:
    async def handle_DATA(self, server, session, envelope):
        mail_trace.new_trace()
        peer_ip = ""
        try:
            peer_ip = (session.peer or ("",))[0]
        except Exception:                                     # noqa: BLE001
            pass
        sender = envelope.mail_from or ""
        recipients = list(envelope.rcpt_tos or [])
        raw: bytes = envelope.content or b""
        tls = bool(getattr(session, "ssl", None))
        t0 = time.monotonic()

        def _audit(action: str, *, subject: str = "", mid: str = "", error: str | None = None,
                   extern: bool = False) -> None:
            try:
                mail_audit.log_event(sender=sender, recipients=recipients, subject=subject,
                                     message_id=mid, action=action, size_bytes=len(raw),
                                     processing_ms=int((time.monotonic() - t0) * 1000),
                                     error=error, quelle=peer_ip, tls=tls, extern=extern)
            except Exception as exc:                          # noqa: BLE001
                log.warning("mail_audit: %s nicht protokolliert: %s", action, exc)

        # ── 1. Quelle ────────────────────────────────────────────────────────
        aus_relay_netz = smtp_relay.ist_relay_quelle(peer_ip) if peer_ip else False
        if not aus_relay_netz:
            log.warning("SMTP: %s abgewiesen — nicht in der Geräteliste", peer_ip or "?")
            # Nur bei eingeschaltetem Relay merken: Sonst sammelte die Liste
            # jeden Scanner, der je bis DATA kam, und das eine neue Gerät
            # wäre darin nicht mehr zu finden.
            if peer_ip and settings_store.get("SMTP_RELAY_ENABLED"):
                relay_hosts.merke_abweisung(peer_ip, sender, recipients, "nicht in der Geräteliste")
            return "554 5.7.1 Access denied"

        # ── 2. Absender und Ziel ─────────────────────────────────────────────
        erlaubt, grund, antwort = smtp_relay.pruefe(sender, recipients, peer_ip)
        if not erlaubt:
            log.warning("%s", grund)
            _audit("relay_abgelehnt", error=grund)
            relay_hosts.merke_abweisung(peer_ip, sender, recipients, grund)
            return antwort

        # ── 3. Zustellen ─────────────────────────────────────────────────────
        # Fürs Dashboard: ging etwas nach draussen? Gemessen an den bekannten
        # Adressen — dieselbe Frage, die `pruefe()` für die Zielgrenze stellt.
        import exo_mailboxes
        bekannt = exo_mailboxes.known_addresses()
        extern = any((r or "").strip().lower() not in bekannt for r in recipients)
        subject, mid = "", ""
        try:
            msg = email.message_from_bytes(raw)
            subject = _decode_subject(msg.get("Subject", ""))
            mid = (msg.get("Message-ID") or "").strip()
        except Exception:                                     # noqa: BLE001
            pass
        log.info("Eingang: von %s from=%s to=%s subject=%r tls=%s",
                 peer_ip, sender, recipients, subject[:80], tls)
        inhalt = _received_header(peer_ip, tls) + raw
        try:
            # smtplib blockiert — nicht auf der Ereignisschleife des Listeners.
            await asyncio.to_thread(smarthost.send, sender, recipients, inhalt)
        except Exception as exc:                              # noqa: BLE001
            _audit("relay_fehler", subject=subject, mid=mid, error=str(exc)[:300], extern=extern)
            # 4xx: Das Gerät soll es erneut versuchen. Die Post ist NICHT
            # angenommen — lieber ein Wiederversuch als ein stiller Verlust.
            return "451 4.4.1 Zustellung an Exchange Online fehlgeschlagen, bitte erneut versuchen"

        relay_hosts.merke_zustellung(peer_ip, tls=tls)
        _audit("relay", subject=subject, mid=mid, extern=extern)
        log.info("SMTP-Relay: %s → %s (von %s)", sender, ", ".join(recipients[:3]), peer_ip)
        return "250 OK"
