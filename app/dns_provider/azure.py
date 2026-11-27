# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Alexander Zarenko / Alexander Zarenko IT Consulting (AZITC)
# Herkunft: EXO SMTP Relay (github.com/azitc-ac/exo-smtp-relay), app/dns_provider/.
# Teil des gemeinsamen DNS-Provider-Katalogs — siehe __init__.py und KATALOG_VERSION.
"""Azure DNS über Azure Resource Manager (REST, api-version 2018-05-01).

⚠️ VORBEREITET, nicht gegen ein echtes Azure erprobt (Stand 27.11.2026). Die
Aufrufe folgen dem Posh-ACME-Plugin Azure.ps1 (in AcmeExchange), das dort im
Einsatz ist:

* Token: App-Registrierung mit Geheimnis (client_credentials gegen
  login.microsoftonline.com, Scope ``https://management.azure.com/.default``)
  oder verwaltete Identität (IMDS ``169.254.169.254`` — nur auf einer Azure-VM).
* Zonen: ``GET /subscriptions/{sub}[/resourceGroups/{rg}]/providers/Microsoft.Network/dnszones``.
* TXT: EIN Record-Set je Name mit mehreren Werten
  (``…/dnszones/{zone}/TXT/{relativer Name}``, ``properties.TXTRecords``).
  Anlegen = Set lesen, unseren Wert anhängen, mit ``If-Match: <etag>`` (bzw.
  ``If-None-Match: *`` bei neuem Set) schreiben. Entfernen = nur unseren Wert
  herausnehmen; ist danach nichts mehr übrig, das Set löschen.

Nötige Rolle: „DNS Zone Contributor“ auf der Zone bzw. Ressourcengruppe.
"""
from __future__ import annotations

from urllib.parse import quote

from .basis import AnmeldungFehler, DnsProvider, DnsProviderFehler, Feld

ARM = "https://management.azure.com"
LOGIN = "https://login.microsoftonline.com"
IMDS = "http://169.254.169.254/metadata/identity/oauth2/token"
API_VERSION = "2018-05-01"
TTL = 60


class AzureProvider(DnsProvider):
    NAME = "azure"
    TITEL = "Azure DNS"
    VORBEREITET = True
    FELDER = (
        Feld("subscription", "Subscription-ID"),
        Feld("resource_group", "Ressourcengruppe", pflicht=False,
             hinweis="leer = alle Zonen der Subscription"),
        Feld("zone", "Zone", pflicht=False, hinweis="leer = aus dem Namen ermitteln"),
        Feld("auth", "Anmeldung", auswahl=("app", "managed"),
             hinweis="app = App-Registrierung mit Geheimnis, managed = verwaltete Identität (Azure-VM)"),
        Feld("tenant", "Tenant-ID", pflicht=False),
        Feld("client_id", "Client-ID der App", pflicht=False,
             hinweis="bei managed optional: benutzerzugewiesene Identität"),
        Feld("client_secret", "Client-Geheimnis", geheim=True, pflicht=False),
    )

    def __init__(self, werte: dict):
        werte = dict(werte)
        werte["auth"] = (str(werte.get("auth") or "app").strip().lower())
        super().__init__(werte)
        if self.werte["auth"] not in ("app", "managed"):
            raise DnsProviderFehler("Azure DNS: Anmeldung muss app oder managed sein")
        if self.werte["auth"] == "app":
            fehlend = [f.titel for f in self.FELDER
                       if f.schluessel in ("tenant", "client_id", "client_secret") and not self.werte[f.schluessel]]
            if fehlend:
                raise DnsProviderFehler(f"Azure DNS: Angabe fehlt — {', '.join(fehlend)}")
        self._token: str | None = None
        self._zonen_rg: dict[str, str] | None = None

    # ── Anmeldung ────────────────────────────────────────────────────────────

    def _kopf(self) -> dict:
        if self._token is None:
            with self._client() as c:
                if self.werte["auth"] == "managed":
                    params = {"api-version": "2018-02-01", "resource": f"{ARM}/"}
                    if self.werte["client_id"]:
                        params["client_id"] = self.werte["client_id"]
                    r = c.get(IMDS, params=params, headers={"Metadata": "true"})
                else:
                    r = c.post(f"{LOGIN}/{quote(self.werte['tenant'])}/oauth2/v2.0/token", data={
                        "grant_type": "client_credentials", "client_id": self.werte["client_id"],
                        "client_secret": self.werte["client_secret"], "scope": f"{ARM}/.default"})
            if r.status_code in (400, 401, 403):
                raise self._fehler(AnmeldungFehler, f"Azure DNS: Anmeldung abgelehnt (HTTP {r.status_code}) "
                                                    f"{(r.text or '')[:300]}")
            self._antwort_pruefen(r, "Anmeldung")
            try:
                self._token = r.json()["access_token"]
            except (ValueError, KeyError) as exc:
                raise DnsProviderFehler(f"Azure DNS: keine Token-Antwort ({type(exc).__name__})")
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def geheimnisse(self) -> list[str]:
        g = super().geheimnisse()
        if self._token:
            g.append(self._token)
        return g

    # ── Zonen ────────────────────────────────────────────────────────────────

    def _basis(self, rg: str | None = None) -> str:
        p = f"{ARM}/subscriptions/{quote(self.werte['subscription'])}"
        rg = rg if rg is not None else self.werte["resource_group"]
        if rg:
            p += f"/resourceGroups/{quote(rg)}"
        return p + "/providers/Microsoft.Network/dnszones"

    def _alle_zonen(self) -> dict[str, str]:
        if self._zonen_rg is None:
            zonen: dict[str, str] = {}
            url = f"{self._basis()}?api-version={API_VERSION}"
            with self._client() as c:
                while url:
                    r = c.get(url, headers=self._kopf())
                    self._antwort_pruefen(r, "Zonen abfragen")
                    daten = r.json()
                    for z in daten.get("value") or []:
                        # id: /subscriptions/…/resourceGroups/<rg>/providers/…
                        teile = str(z.get("id", "")).split("/")
                        rg = teile[teile.index("resourceGroups") + 1] if "resourceGroups" in teile else ""
                        zonen[str(z["name"]).strip(".").lower()] = rg
                    url = daten.get("nextLink") or ""
            self._zonen_rg = zonen
        return self._zonen_rg

    def _zonen(self) -> list[str]:
        return sorted(self._alle_zonen())

    def _set_url(self, name: str) -> str:
        zone = self.werte["zone"].strip(".").lower() or self.zone_fuer(name)
        rg = self.werte["resource_group"] or self._alle_zonen().get(zone, "")
        if not rg:
            raise DnsProviderFehler(f"Azure DNS: Ressourcengruppe der Zone {zone} unbekannt")
        if not name.endswith("." + zone) and name != zone:
            raise DnsProviderFehler(f"Azure DNS: {name} liegt nicht in der Zone {zone}")
        rel = name[: -len(zone) - 1] if name != zone else "@"
        return f"{self._basis(rg)}/{quote(zone)}/TXT/{quote(rel)}?api-version={API_VERSION}"

    # ── TXT ──────────────────────────────────────────────────────────────────

    def _lesen(self, url: str) -> tuple[list[list[str]], str | None]:
        with self._client() as c:
            r = c.get(url, headers=self._kopf())
        if r.status_code == 404:
            return [], None
        self._antwort_pruefen(r, "TXT-Set lesen")
        d = r.json()
        werte = [list(t.get("value") or []) for t in (d.get("properties") or {}).get("TXTRecords") or []]
        return werte, d.get("etag")

    def _txt_anlegen(self, name: str, wert: str) -> None:
        url = self._set_url(name)
        werte, etag = self._lesen(url)
        if any("".join(w) == wert for w in werte):
            return
        werte.append([wert])
        kopf = self._kopf() | ({"If-Match": etag} if etag else {"If-None-Match": "*"})
        with self._client() as c:
            r = c.put(url, headers=kopf, json={"properties": {"TTL": TTL, "TXTRecords":
                                                               [{"value": w} for w in werte]}})
        self._antwort_pruefen(r, "TXT-Set schreiben")

    def _txt_entfernen(self, name: str, wert: str) -> None:
        url = self._set_url(name)
        werte, etag = self._lesen(url)
        rest = [w for w in werte if "".join(w) != wert]
        if len(rest) == len(werte):
            return
        kopf = self._kopf() | ({"If-Match": etag} if etag else {})
        with self._client() as c:
            if rest:
                r = c.put(url, headers=kopf, json={"properties": {"TTL": TTL, "TXTRecords":
                                                                   [{"value": w} for w in rest]}})
            else:
                r = c.delete(url, headers=kopf)
        self._antwort_pruefen(r, "TXT-Set schreiben")
