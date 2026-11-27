# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Alexander Zarenko / Alexander Zarenko IT Consulting (AZITC)
# Herkunft: EXO SMTP Relay (github.com/azitc-ac/exo-smtp-relay), app/dns_provider/.
# Teil des gemeinsamen DNS-Provider-Katalogs — siehe __init__.py und KATALOG_VERSION.
"""Basisklasse und Fehlerarten des DNS-Provider-Katalogs.

Ein Provider kann genau vier Dinge: Zonen auflisten (nur lesend, für „Verbindung
testen“), die Zone zu einem Namen finden, EINEN TXT-Wert anlegen und GENAU
DIESEN Wert wieder entfernen. Fremde TXT-Werte unter demselben Namen (SPF,
Domänen-Nachweise, ein zweiter ACME-Wert für Wildcard und Apex) bleiben stehen.

Keine Abhängigkeit auf die Anwendung: Werte kommen als Wörterbuch herein, die
Anwendung legt sie ab und schützt sie. Einzige Fremdbibliothek ist httpx.

GEHEIMNISSE: Kein Provider schreibt ein Geheimnis in eine Meldung. Jede
Ausnahme, die ein Provider wirft, läuft durch `schwaerzen()` — auch wenn ein
Fremdsystem den Schlüssel in seiner Antwort wiederholt.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx


class DnsProviderFehler(Exception):
    """Allgemeiner Fehler eines Providers (Meldung ist geschwärzt)."""


class AnmeldungFehler(DnsProviderFehler):
    """Zugangsdaten falsch oder ohne Recht (HTTP 401/403)."""


class ZoneNichtGefunden(DnsProviderFehler):
    """Für den Namen gibt es beim Provider keine passende Zone."""


@dataclass(frozen=True)
class Feld:
    """Eine Angabe, die ein Provider braucht.

    schluessel: Name im Wertewörterbuch (die Anwendung bildet daraus ihren
    Einstellungsnamen). geheim: nie anzeigen, nie protokollieren, nur maskiert.
    """
    schluessel: str
    titel: str
    geheim: bool = False
    pflicht: bool = True
    hinweis: str = ""
    auswahl: tuple[str, ...] = ()       # feste Werte (Auswahlliste), sonst Freitext


def zone_waehlen(name: str, zonen: list[str]) -> str | None:
    """Die tiefste Zone, in der `name` liegt (wie Posh-ACME Find-IONOSZone):
    für _acme-challenge.a.b.example.com zuerst a.b.example.com, dann
    b.example.com, dann example.com."""
    name = name.strip(".").lower()
    vorhanden = {z.strip(".").lower() for z in zonen}
    teile = name.split(".")
    for i in range(len(teile) - 1):
        kandidat = ".".join(teile[i:])
        if kandidat in vorhanden:
            return kandidat
    return None


class DnsProvider:
    """Basisklasse. Unterklassen setzen NAME, TITEL, FELDER und die vier
    Methoden `_zonen`, `_txt_anlegen`, `_txt_entfernen` (und optional
    `_vorbereiten`)."""

    NAME = ""
    TITEL = ""
    FELDER: tuple[Feld, ...] = ()
    VORBEREITET = False          # True: Code vorhanden, aber nicht gegen den echten Dienst erprobt
    # Für Tests: ein httpx-Transport (z. B. httpx.MockTransport). None = echtes Netz.
    transport: httpx.BaseTransport | None = None
    # TLS-Prüfung der API-Verbindung: True = certifi (httpx-Vorgabe) oder ein
    # ssl.SSLContext der Anwendung (Relay: tls_vertrauen.client_kontext(), weil
    # Windows Wurzelzertifikate erst bei Bedarf nachlädt).
    verify = True
    ZEITLIMIT = 20.0

    def __init__(self, werte: dict):
        self.werte = {f.schluessel: str(werte.get(f.schluessel) or "").strip() for f in self.FELDER}
        fehlend = [f.titel for f in self.FELDER if f.pflicht and not self._pflicht_erfuellt(f)]
        if fehlend:
            raise DnsProviderFehler(f"{self.TITEL}: Angabe fehlt — {', '.join(fehlend)}")

    def _pflicht_erfuellt(self, f: Feld) -> bool:
        return bool(self.werte.get(f.schluessel))

    # ── Geheimnisse ──────────────────────────────────────────────────────────

    def geheimnisse(self) -> list[str]:
        """Alle Werte, die nie in einer Meldung stehen dürfen (auch zusammengesetzte)."""
        return [self.werte[f.schluessel] for f in self.FELDER
                if f.geheim and self.werte.get(f.schluessel)]

    def schwaerzen(self, text: str) -> str:
        text = str(text)
        for g in sorted(self.geheimnisse(), key=len, reverse=True):
            if g:
                text = text.replace(g, "••••")
        return text

    def _fehler(self, art: type[DnsProviderFehler], text: str) -> DnsProviderFehler:
        return art(self.schwaerzen(text))

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.ZEITLIMIT, transport=self.transport, verify=self.verify)

    # ── Öffentliche Schnittstelle (wirft nur DnsProviderFehler) ──────────────

    def zonen(self) -> list[str]:
        """Nur lesend — für „Verbindung testen“."""
        return self._sicher(self._zonen)

    def zone_fuer(self, name: str) -> str:
        zone = zone_waehlen(name, self.zonen())
        if not zone:
            raise ZoneNichtGefunden(self.schwaerzen(
                f"{self.TITEL}: keine Zone für {name.strip('.')} in diesem Konto"))
        return zone

    def txt_anlegen(self, name: str, wert: str) -> None:
        """TXT-Wert `wert` unter `name` (FQDN) anlegen. Ist er schon da: nichts tun."""
        self._sicher(self._txt_anlegen, name.strip(".").lower(), wert)

    def txt_entfernen(self, name: str, wert: str) -> None:
        """NUR den Wert `wert` unter `name` entfernen; fremde Werte bleiben."""
        self._sicher(self._txt_entfernen, name.strip(".").lower(), wert)

    def _sicher(self, fn, *args):
        try:
            return fn(*args)
        except DnsProviderFehler as exc:
            raise type(exc)(self.schwaerzen(str(exc))) from None
        except httpx.TimeoutException:
            raise DnsProviderFehler(f"{self.TITEL}: Zeitüberschreitung bei der API") from None
        except httpx.HTTPError as exc:
            raise DnsProviderFehler(self.schwaerzen(
                f"{self.TITEL}: Verbindung zur API fehlgeschlagen ({type(exc).__name__}: {exc})")) from None

    # ── Von Unterklassen zu liefern ──────────────────────────────────────────

    def _zonen(self) -> list[str]:                     # pragma: no cover - abstrakt
        raise NotImplementedError

    def _txt_anlegen(self, name: str, wert: str) -> None:   # pragma: no cover - abstrakt
        raise NotImplementedError

    def _txt_entfernen(self, name: str, wert: str) -> None:  # pragma: no cover - abstrakt
        raise NotImplementedError

    def _antwort_pruefen(self, r: httpx.Response, was: str) -> None:
        """401/403 → AnmeldungFehler, sonst ≥ 400 → DnsProviderFehler. Die
        Antwort wird gekürzt und geschwärzt wiedergegeben."""
        if r.status_code < 400:
            return
        text = (r.text or "")[:300]
        if r.status_code in (401, 403):
            raise self._fehler(AnmeldungFehler,
                               f"{self.TITEL}: Zugang abgelehnt (HTTP {r.status_code}) bei {was} — "
                               f"Schlüssel bzw. Rechte prüfen. {text}")
        raise self._fehler(DnsProviderFehler, f"{self.TITEL}: {was} fehlgeschlagen (HTTP {r.status_code}) {text}")
