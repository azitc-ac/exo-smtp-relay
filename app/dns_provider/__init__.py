# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Alexander Zarenko / Alexander Zarenko IT Consulting (AZITC)
# Herkunft: EXO SMTP Relay (github.com/azitc-ac/exo-smtp-relay), app/dns_provider/.
"""DNS-Provider-Katalog für ACME DNS-01 — gemeinsam für Relay und Gateway.

Entstanden im Relay („Relay zuerst“, 27.11.2026); wandert UNVERÄNDERT ins
Gateway. Deshalb:

* keine Abhängigkeit auf die Anwendung (kein config, settings_store, log der
  Anwendung) — nur Standardbibliothek und httpx;
* nur relative Importe innerhalb des Pakets;
* `KATALOG_VERSION` trägt Fassung und SHA-256 jeder Datei. Ein Test in jeder
  Anwendung vergleicht Manifest und Inhalt; wer eine Datei ändert, schreibt
  das Manifest neu (``python -m dns_provider schreiben`` aus app/) und hebt
  die Fassung an. Im Gateway kommt beim Port ein Gleichlauf-Test hinzu.

Provider: IONOS (erprobt), Azure DNS (vorbereitet). Ein neuer Provider ist
eine Unterklasse von `basis.DnsProvider` plus ein Eintrag in `PROVIDER`.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .azure import AzureProvider
from .basis import AnmeldungFehler, DnsProvider, DnsProviderFehler, Feld, ZoneNichtGefunden, zone_waehlen
from .ionos import IonosProvider

__all__ = ["PROVIDER", "erzeugen", "DnsProvider", "DnsProviderFehler", "AnmeldungFehler",
           "ZoneNichtGefunden", "Feld", "zone_waehlen", "manifest_berechnen", "manifest_lesen"]

PROVIDER: dict[str, type[DnsProvider]] = {
    IonosProvider.NAME: IonosProvider,
    AzureProvider.NAME: AzureProvider,
}

ORDNER = Path(__file__).resolve().parent
MANIFEST = ORDNER / "KATALOG_VERSION"


def erzeugen(name: str, werte: dict) -> DnsProvider:
    cls = PROVIDER.get((name or "").strip().lower())
    if cls is None:
        raise DnsProviderFehler(f"Unbekannter DNS-Provider: {name!r}")
    return cls(werte)


# ── Manifest ─────────────────────────────────────────────────────────────────

def _dateien() -> list[Path]:
    return sorted(p for p in ORDNER.glob("*.py"))


def _summe(pfad: Path) -> str:
    # Zeilenenden vereinheitlichen: ein Windows-Checkout mit CRLF ist derselbe Inhalt.
    return hashlib.sha256(pfad.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def manifest_lesen() -> tuple[str, dict[str, str]]:
    """(Fassung, {Datei: SHA-256}) aus KATALOG_VERSION."""
    fassung, summen = "", {}
    for zeile in MANIFEST.read_text(encoding="utf-8").splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#"):
            continue
        if zeile.startswith("version "):
            fassung = zeile.split(None, 1)[1]
            continue
        summe, datei = zeile.split(None, 1)
        summen[datei.strip()] = summe
    return fassung, summen


def manifest_berechnen() -> dict[str, str]:
    return {p.name: _summe(p) for p in _dateien()}


def manifest_schreiben(fassung: str) -> None:
    zeilen = ["# DNS-Provider-Katalog — Fassung und SHA-256 je Datei (LF-normalisiert).",
              "# Neu schreiben: cd app && python -m dns_provider schreiben <fassung>",
              f"version {fassung}"]
    zeilen += [f"{s}  {d}" for d, s in manifest_berechnen().items()]
    MANIFEST.write_text("\n".join(zeilen) + "\n", encoding="utf-8")


__version__ = manifest_lesen()[0] if MANIFEST.exists() else ""
