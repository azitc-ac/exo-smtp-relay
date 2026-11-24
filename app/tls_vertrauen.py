"""Vertrauensanker für AUSGEHENDE TLS-Verbindungen — ein Pfad für alle.

WARUM: Windows lädt Wurzelzertifikate erst bei Bedarf über CryptoAPI nach
(„Automatic Root Certificates Update“). Python liest nur, was im Speicher
schon liegt. Auf einem frischen Windows Server fehlt dort z. B. die Wurzel,
unter der api.github.com oder Exchange Online hängt — `ssl.create_default_
context()` scheitert dann mit CERTIFICATE_VERIFY_FAILED (Labtest 0.2.42,
Server 2025: „Aktualisierung prüfen“).

DESHALB vertraut der Kontext BEIDEN Quellen:
  - dem Systemspeicher (bleibt — eine Firmen-CA eines internen Smarthosts
    oder eines TLS-prüfenden Proxys steht nur dort),
  - ZUSÄTZLICH dem Mozilla-Bündel aus certifi (im Lock festgenagelt).

Alle ausgehenden Verbindungen (urllib, smtplib-STARTTLS, httpx) holen ihren
Kontext hier. Ausnahme: `acme_client.py` ist eine Spiegeldatei des Gateways
(tools/driftcheck.py) und bleibt unverändert — httpx nimmt dort ohnehin
certifi.
"""
from __future__ import annotations

import logging
import ssl

log = logging.getLogger(__name__)


def client_kontext() -> ssl.SSLContext:
    """Neuer Client-Kontext: Systemspeicher + certifi, strenge Prüfung."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception as exc:                                  # noqa: BLE001
        # Ohne certifi bleibt der Systemspeicher — schlechter, aber kein Absturz.
        log.warning("certifi nicht ladbar, nur Systemspeicher: %s", exc)
    return ctx
