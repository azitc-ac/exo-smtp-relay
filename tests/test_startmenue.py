"""Startmenü-Link folgt dem Hostnamen (nur Windows, wenn der Installer ihn angelegt hat).

Der Hostname aus dem Assistenten ist der Name, unter dem EXCHANGE den Dienst erreicht. Auf dem
Server selbst muss er nicht auflösbar sein — dann zeigt der Link auf den Rechnernamen oder localhost.
"""
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

ALT = b"[InternetShortcut]\r\nURL=https://localhost:8443/\r\n"


@pytest.fixture
def link(tmp_path, monkeypatch):
    import startmenue
    p = tmp_path / "EXO SMTP Relay.url"
    monkeypatch.setattr(startmenue, "_pfad", lambda: p)
    monkeypatch.setattr(startmenue.sys, "platform", "win32")
    monkeypatch.setattr(startmenue.config, "WEBUI_PORT", 8443)
    monkeypatch.setattr(startmenue.socket, "gethostname", lambda: "SRV2")
    return p


def _dns(monkeypatch, bekannt):
    import startmenue
    monkeypatch.setattr(startmenue, "_loest_auf", lambda name: name in bekannt)


def test_ohne_installer_link_passiert_nichts(link, monkeypatch):
    import startmenue
    _dns(monkeypatch, {"relay.firma.de"})
    assert startmenue.aktualisieren("relay.firma.de") is False and not link.exists()


def test_link_folgt_dem_hostnamen_wenn_er_aufloest(link, monkeypatch):
    import startmenue
    _dns(monkeypatch, {"relay.firma.de", "SRV2"})
    link.write_bytes(ALT)
    assert startmenue.aktualisieren("relay.firma.de") is True
    assert link.read_bytes() == b"[InternetShortcut]\r\nURL=https://relay.firma.de:8443/\r\n"
    assert startmenue.aktualisieren("relay.firma.de") is False, "unveraendert: nicht neu schreiben"


def test_nicht_aufloesbarer_hostname_faellt_auf_den_rechnernamen_zurueck(link, monkeypatch):
    """Genau der Fall aus der Abnahme: srv2.home.local gab es in keinem DNS (DNS_PROBE_FINISHED_NXDOMAIN)."""
    import startmenue
    _dns(monkeypatch, {"SRV2"})
    link.write_bytes(ALT)
    assert startmenue.aktualisieren("srv2.home.local") is True
    assert link.read_bytes() == b"[InternetShortcut]\r\nURL=https://SRV2:8443/\r\n"


def test_ohne_jede_aufloesung_bleibt_localhost(link, monkeypatch):
    import startmenue
    _dns(monkeypatch, set())
    link.write_bytes(b"[InternetShortcut]\r\nURL=https://SRV2:8443/\r\n")
    assert startmenue.aktualisieren("relay.firma.de") is True
    assert link.read_bytes() == ALT


def test_leerer_hostname_aendert_nichts(link, monkeypatch):
    import startmenue
    _dns(monkeypatch, {"SRV2"})
    link.write_bytes(ALT)
    assert startmenue.aktualisieren("  ") is False and b"localhost" in link.read_bytes()
