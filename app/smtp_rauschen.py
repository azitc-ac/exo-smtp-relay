"""Abgebrochene Fremdverbindungen auf Port 25 sind kein Betriebsfehler.

ANLASS (24.08.2026)
-------------------
Im Protokoll der Produktions-VM waren **143 von 144 Zeilen der Stufe ERROR**
abgebrochene Verbindungen fremder Rechner — durchweg mit vollständigem
Traceback, durchweg beim TLS-Handshake:

    ERROR mail.log: ('69.164.214.31', 61000) SMTP session exception
    Traceback (most recent call last):
      File ".../aiosmtpd/smtp.py", line 936, in smtp_STARTTLS
    …

Wer im Protokoll nach einem echten Fehler sucht, findet 143 Port-Scanner und
einen Befund. Damit ist die Stufe ERROR als Suchmerkmal wertlos — dieselbe
Wirkung wie bei einer Prüfung, die immer anschlägt.

Gefährlich sind diese Verbindungen nicht: Die Quell-IP-Prüfung sitzt in
`handler.handle_DATA`, und die Scanner kommen nie so weit — sie brechen vorher
ab. Es ist ausschliesslich ein Diagnoseproblem.

WARUM EIN FILTER UND NICHT DER HANDLER-HOOK
-------------------------------------------
`aiosmtpd.SMTP.handle_exception()` ruft, sofern vorhanden,
`event_handler.handle_exception(error)` — das wäre der vorgesehene
Erweiterungspunkt. Er bekommt aber **nur die Ausnahme, nicht die Sitzung**, und
damit ginge die Gegenstelle verloren. Gerade die will man wissen, wenn aus dem
Rauschen einmal ein Angriff wird. Der Filter formt die Meldung um und behält
sie.

Fehlermodus, falls aiosmtpd seinen Wortlaut ändert: Der Filter greift nicht
mehr, und es steht wieder alles wie heute im Protokoll. Nichts wird
verschluckt, nichts bricht — der Zustand ist dann nur wieder laut.
"""
from __future__ import annotations

import asyncio
import logging
import ssl

MELDUNG = "SMTP session exception"

# Abbrüche, die eine Gegenstelle jederzeit herbeiführen darf, ohne dass an
# diesem Gateway etwas falsch wäre: kein TLS zustande gekommen, Verbindung
# zugemacht, nichts mehr gesendet.
HARMLOS: tuple[type[BaseException], ...] = (
    ssl.SSLError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,
    asyncio.IncompleteReadError,
)


def _harmlose_ursache(fehler: BaseException | None) -> BaseException | None:
    """Die harmlose Ausnahme in der Kette, oder None.

    ⚠️ aiosmtpd hüllt JEDEN Fehler beim STARTTLS-Handshake ein:
    `raise TLSSetupException() from error` (smtp.py, smtp_STARTTLS, seit 1.4).
    Im Protokoll kommt also nie `SSLError` an, sondern die Hülle — die
    eigentliche Ursache steht in `__cause__`. Bis 06.10.2026 prüfte der Filter
    nur die äusserste Ausnahme und griff deshalb bei genau dem Fall, für den er
    gebaut war, NIE (Produktions-VM: 0 umgeformte Zeilen in 96 h, alle
    Scanner-Abbrüche weiter als ERROR mit Traceback). Sein Test baute die
    Ausnahme selbst und sah die Hülle nie.

    Gefolgt wird nur `__cause__` (ausdrückliches `from`), höchstens fünf Glieder.
    Eine Hülle um einen ECHTEN Fehler bleibt damit ein Fehler."""
    for _ in range(5):
        if fehler is None:
            return None
        if isinstance(fehler, HARMLOS):
            return fehler
        fehler = fehler.__cause__
    return None


class AbbruchLeiser(logging.Filter):
    """Stuft abgebrochene Fremdverbindungen auf INFO herab, ohne Traceback.

    ⚠️ Unterdrückt nichts. Die Zeile bleibt — mit Gegenstelle und Grund, nur
    einzeilig und nicht mehr als Fehler. Wer Scanner-Aktivität sehen will,
    liest INFO; wer einen Betriebsfehler sucht, findet ihn wieder unter ERROR.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if MELDUNG not in str(record.msg):
            return True
        fehler = _harmlose_ursache(record.exc_info[1] if record.exc_info else None)
        if fehler is None:
            return True          # echter Fehler — unverändert weiterreichen

        gegenstelle = record.args[0] if record.args else "?"
        record.levelno = logging.INFO
        record.levelname = "INFO"
        record.exc_info = None
        record.exc_text = None   # sonst hängt ein bereits formatierter Traceback an
        record.msg = "SMTP-Verbindung von %r ohne Datenübergabe beendet (%s)"
        record.args = (gegenstelle, type(fehler).__name__)
        return True
