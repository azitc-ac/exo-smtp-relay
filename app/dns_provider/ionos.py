# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Alexander Zarenko / Alexander Zarenko IT Consulting (AZITC)
# Herkunft: EXO SMTP Relay (github.com/azitc-ac/exo-smtp-relay), app/dns_provider/.
# Teil des gemeinsamen DNS-Provider-Katalogs — siehe __init__.py und KATALOG_VERSION.
"""IONOS DNS API (Hosting) — https://api.hosting.ionos.com/dns/v1

Geprüft am 27.11.2026 gegen die offizielle OpenAPI-Beschreibung
(developer.hosting.ionos.de/assets/kms-swagger-specs/dns.yaml, „DNS API“ 1.0.2)
und die Seite „Getting started“ des IONOS Developer Portals:

* Anmeldung: Kopfzeile ``X-API-Key: <öffentliches Präfix>.<Geheimnis>``.
  Den Schlüssel legt man im Developer Portal unter „API Keys“ → „Create New Key“
  an; das Geheimnis zeigt IONOS nur einmal.
* ``GET /v1/zones`` → ``[{id, name, type}]``; 401 bei falschem Schlüssel.
* ``GET /v1/zones/{zoneId}?recordName=…&recordType=TXT`` → ``{records: [...]}``.
* ``POST /v1/zones/{zoneId}/records`` mit einer LISTE ``[{name, type, content,
  ttl, prio, disabled}]`` → 201. ``name`` ist der volle Name (FQDN).
* ``DELETE /v1/zones/{zoneId}/records/{recordId}`` → 200.
* Ratenbegrenzung: 1200 Anfragen je Stunde und Schlüssel (dann 429).

Vorlage für Zonensuche und Vergleich: das Posh-ACME-Plugin IONOS.ps1 (in
AcmeExchange). Dort wie hier: IONOS liefert TXT-Inhalte in Anführungszeichen
zurück (``"wert"``), angelegt wird ohne. Verglichen wird deshalb ohne
Anführungszeichen — sonst fände das Aufräumen den eigenen Wert nicht.

Jeder TXT-Wert ist bei IONOS ein eigener Record mit eigener ID. Entfernt wird
nur der Record mit genau unserem Namen UND Wert.
"""
from __future__ import annotations

from .basis import DnsProvider, DnsProviderFehler, Feld

API = "https://api.hosting.ionos.com/dns/v1"
TTL = 60


def _ohne_anfuehrung(inhalt: str) -> str:
    s = (inhalt or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s


class IonosProvider(DnsProvider):
    NAME = "ionos"
    TITEL = "IONOS"
    FELDER = (
        Feld("prefix", "API-Key: öffentliches Präfix",
             hinweis="IONOS Developer Portal → API Keys → Create New Key"),
        Feld("secret", "API-Key: Geheimnis", geheim=True,
             hinweis="wird von IONOS nur einmal angezeigt"),
    )

    def __init__(self, werte: dict):
        super().__init__(werte)
        self._zonen_ids: dict[str, str] | None = None

    def geheimnisse(self) -> list[str]:
        g = super().geheimnisse()
        if self.werte.get("secret"):
            g.append(f"{self.werte['prefix']}.{self.werte['secret']}")
        return g

    def _kopf(self) -> dict:
        return {"X-API-Key": f"{self.werte['prefix']}.{self.werte['secret']}",
                "Accept": "application/json"}

    def _alle_zonen(self) -> dict[str, str]:
        if self._zonen_ids is None:
            with self._client() as c:
                r = c.get(f"{API}/zones", headers=self._kopf())
            self._antwort_pruefen(r, "Zonen abfragen")
            try:
                self._zonen_ids = {str(z["name"]).strip(".").lower(): str(z["id"]) for z in r.json()}
            except (ValueError, KeyError, TypeError) as exc:
                raise DnsProviderFehler(f"IONOS: unerwartete Antwort auf Zonen ({type(exc).__name__})")
        return self._zonen_ids

    def _zonen(self) -> list[str]:
        return sorted(self._alle_zonen())

    def _zone_id(self, name: str) -> str:
        return self._alle_zonen()[self.zone_fuer(name)]

    def _eigene(self, zone_id: str, name: str, wert: str) -> list[dict]:
        with self._client() as c:
            r = c.get(f"{API}/zones/{zone_id}", headers=self._kopf(),
                      params={"recordName": name, "recordType": "TXT"})
        self._antwort_pruefen(r, "TXT-Records lesen")
        try:
            records = r.json().get("records") or []
        except (ValueError, AttributeError) as exc:
            raise DnsProviderFehler(f"IONOS: unerwartete Antwort auf Records ({type(exc).__name__})")
        # Der Filter recordName ist eine Hilfe, keine Zusage — selbst nachprüfen.
        return [rec for rec in records
                if str(rec.get("type", "")).upper() == "TXT"
                and str(rec.get("name", "")).strip(".").lower() == name
                and _ohne_anfuehrung(str(rec.get("content", ""))) == wert]

    def _txt_anlegen(self, name: str, wert: str) -> None:
        zone_id = self._zone_id(name)
        if self._eigene(zone_id, name, wert):
            return
        with self._client() as c:
            r = c.post(f"{API}/zones/{zone_id}/records", headers=self._kopf(),
                       json=[{"name": name, "type": "TXT", "content": wert,
                              "ttl": TTL, "prio": 0, "disabled": False}])
        self._antwort_pruefen(r, "TXT-Record anlegen")

    def _txt_entfernen(self, name: str, wert: str) -> None:
        zone_id = self._zone_id(name)
        for rec in self._eigene(zone_id, name, wert):
            with self._client() as c:
                r = c.delete(f"{API}/zones/{zone_id}/records/{rec['id']}", headers=self._kopf())
            if r.status_code != 404:            # schon weg: Ziel erreicht
                self._antwort_pruefen(r, "TXT-Record löschen")
