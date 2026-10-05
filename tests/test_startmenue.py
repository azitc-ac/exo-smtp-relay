"""Startmenü-Link folgt dem Hostnamen (nur Windows, wenn der Installer ihn angelegt hat)."""
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))


@pytest.fixture
def link(tmp_path, monkeypatch):
    import startmenue
    p = tmp_path / "EXO SMTP Relay.url"
    monkeypatch.setattr(startmenue, "_pfad", lambda: p)
    monkeypatch.setattr(startmenue.sys, "platform", "win32")
    monkeypatch.setattr(startmenue.config, "WEBUI_PORT", 8443)
    return p


def test_ohne_installer_link_passiert_nichts(link):
    import startmenue
    assert startmenue.aktualisieren("relay.firma.de") is False and not link.exists()


def test_link_folgt_dem_hostnamen(link):
    import startmenue
    link.write_bytes(b"[InternetShortcut]\r\nURL=https://localhost:8443/\r\n")
    assert startmenue.aktualisieren("Relay.Firma.de".lower()) is True
    assert link.read_bytes() == b"[InternetShortcut]\r\nURL=https://relay.firma.de:8443/\r\n"
    assert startmenue.aktualisieren("relay.firma.de") is False, "unveraendert: nicht neu schreiben"


def test_leerer_hostname_aendert_nichts(link):
    import startmenue
    link.write_bytes(b"[InternetShortcut]\r\nURL=https://localhost:8443/\r\n")
    assert startmenue.aktualisieren("  ") is False and b"localhost" in link.read_bytes()
