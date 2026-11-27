"""STARTTLS: für Geräte keine Pflicht — und wer nicht freigegeben ist, kommt nicht durch.

STAND 0.2.62: Bis 0.2.61 galt die Pflicht für jede nicht freigegebene Adresse.
Geschützt hat sie dort nur die Befehle einer Sitzung, die ohnehin abgewiesen
wurde — Annahme setzt in `handler.handle_DATA` immer die Freigabe voraus. Dafür
las ein nicht eingetragenes Gerät zuerst „Must issue a STARTTLS command first“
und suchte den Fehler am Zertifikat. Jetzt wird es bei RCPT mit Grund
abgewiesen, ohne STARTTLS (`handler.handle_RCPT`). Pflicht bleibt STARTTLS nur
für die eigenen Adressen des Servers (Testmail-Weg). Die Gegenrichtung prüfen
die Tests unten weiter — nur ist die Schutzfunktion jetzt die Abweisung.

ANLASS (2026-08-25)
-------------------
Ein Etikettendrucker von 2011 kann kein STARTTLS, ein Scanner nur TLS 1.0.
Genau diese Geräte sind der Grund für das Relay; bestünde die Pflicht, liefe
das Feature für seinen Hauptanwendungsfall nicht.

⚠️ WARUM DAS DER GEFÄHRLICHSTE TEST DIESER REIHE IST
Hier wird eine Schutzfunktion **entfernt**, nicht hinzugefügt. Greift die
Begrenzung nicht, nimmt der Listener von jedem unverschlüsselt entgegen — und
zwar still: Der Mailfluss funktioniert weiter, die Protokolle sehen normal aus,
nichts schlägt an. Aufzufallen bräuchte es jemanden, der mitliest.

Die Tests prüfen deshalb vor allem die Gegenrichtung: dass die Pflicht überall
sonst bestehen bleibt — für Exchange, für unbekannte Adressen, für gesperrte
Geräte und nach dem Ende eines Lernlaufs.
"""
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import main  # noqa: E402


class _Transport:
    def __init__(self, ip):
        self._ip = ip

    def get_extra_info(self, name, default=None):
        return (self._ip, 51234) if name == "peername" else default


class _Sitzung:
    peer = None
    ssl = None


@pytest.fixture
def listener(monkeypatch, tmp_path):
    """Ein Listener mit TLS-Pflicht und einem eingetragenen Gerät."""
    werte = {
        "SMTP_RELAY_ENABLED": True,
        "SMTP_RELAY_LERN_NETZE": [],
        "SMTP_RELAY_LERN_BIS": "",
        "TENANT_DOMAIN": "firma.onmicrosoft.com",
    }
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))

    import relay_hosts
    monkeypatch.setattr(relay_hosts, "DB_PATH", tmp_path / "relay_hosts.db")
    relay_hosts.speichern("10.1.5.7", kommentar="Etikettendrucker")

    def bauen(ip):
        """Eine Instanz, wie sie beim Verbindungsaufbau entsteht.

        `connection_made` wird direkt gerufen statt eine echte Verbindung
        aufzubauen: Gemessen werden soll die ENTSCHEIDUNG, nicht aiosmtpd.
        """
        smtp = main._LenientSMTP.__new__(main._LenientSMTP)
        smtp.require_starttls = True
        smtp.session = _Sitzung()

        def _super_ersatz(transport):
            smtp.session.peer = transport.get_extra_info("peername")

        monkeypatch.setattr(main._BaseSMTP, "connection_made",
                            lambda self, t: _super_ersatz(t))
        smtp.connection_made(_Transport(ip))
        return smtp

    return bauen, werte, relay_hosts


def _rcpt(ip):
    """handle_RCPT für eine Verbindung von `ip` → (Antwort, angenommene Empfänger)."""
    import asyncio
    import handler

    class _Umschlag:
        mail_from = "drucker@firma.de"
        rcpt_tos: list = []
        rcpt_options: list = []
    s, u = _Sitzung(), _Umschlag()
    u.rcpt_tos, u.rcpt_options = [], []
    s.peer = (ip, 51234)
    antwort = asyncio.run(handler.RelayHandler().handle_RCPT(None, s, u, "chefin@firma.de", []))
    return antwort, u.rcpt_tos


def _abgewiesen(ip):
    antwort, angenommen = _rcpt(ip)
    return antwort.startswith("554 5.7.1") and "nicht freigegeben" in antwort and angenommen == []


def test_eingetragenes_geraet_darf_ohne_starttls(listener):
    bauen, _, _ = listener
    assert bauen("10.1.5.7").require_starttls is False
    assert _rcpt("10.1.5.7") == ("250 OK", ["chefin@firma.de"])


def test_fremde_adresse_wird_bei_rcpt_abgewiesen(listener):
    """⚠️ Der wichtigste Test: Eine Adresse von aussen (hier ein Bereich von Microsoft)
    kommt ohne Eintrag nicht durch — die Lockerung der TLS-Pflicht darf hier nichts öffnen."""
    bauen, _, _ = listener
    assert bauen("52.101.42.11").require_starttls is False
    assert _abgewiesen("52.101.42.11")


def test_unbekannte_adresse_wird_abgewiesen(listener):
    bauen, _, _ = listener
    assert _abgewiesen("10.1.5.99"), (
        "Nachbar im selben Netz, aber nicht eingetragen — die Liste ist die Freigabe.")


def test_gesperrtes_geraet_wird_abgewiesen(listener):
    """Eine Sperre wirkt sofort, auch ohne STARTTLS-Pflicht."""
    _, _, relay_hosts = listener
    relay_hosts.speichern("10.1.5.7", gesperrt=True)
    assert _abgewiesen("10.1.5.7")


def test_abgeschaltetes_relay_weist_auch_eingetragene_ab(listener):
    _, werte, _ = listener
    werte["SMTP_RELAY_ENABLED"] = False
    assert _abgewiesen("10.1.5.7")


def test_eigene_adresse_behaelt_die_pflicht(listener):
    """Der Server selbst ist kein Gerät: Testmail-Weg, STARTTLS bleibt Pflicht, und
    entschieden wird erst bei DATA über die Einmal-Kennung (test_testmail.py)."""
    bauen, _, _ = listener
    assert bauen("127.0.0.1").require_starttls is True
    assert _rcpt("127.0.0.1") == ("250 OK", ["chefin@firma.de"])


def test_im_lernfenster_darf_ein_neues_geraet_ohne_starttls(listener):
    """Sonst käme ein Gerät ohne STARTTLS nie in die Liste — der Lernlauf liefe
    ins Leere, und zwar genau für die Geräte, um die es geht."""
    from datetime import datetime, timedelta, timezone
    bauen, werte, _ = listener
    werte["SMTP_RELAY_LERN_NETZE"] = ["10.1.5.0/24"]
    werte["SMTP_RELAY_LERN_BIS"] = (datetime.now(timezone.utc)
                                    + timedelta(minutes=10)).isoformat()
    assert bauen("10.1.5.44").require_starttls is False

    assert _rcpt("10.1.5.44")[0] == "250 OK"

    werte["SMTP_RELAY_LERN_BIS"] = ""
    assert _abgewiesen("10.1.5.44"), (
        "Nach dem Lernlauf ist Schluss — für eine Adresse, die es nicht in die Liste geschafft hat.")


def test_ohne_zertifikat_bleibt_alles_wie_es_war(listener, monkeypatch):
    """Gibt es kein Zertifikat, gibt es keine Pflicht — und nichts zu lockern.

    Die Prüfung darf in diesem Fall gar nicht erst laufen: Sie würde bei jeder
    Verbindung die Geräteliste anfassen, ohne dass es etwas ändern kann.
    """
    import smtp_relay
    gefragt = []
    monkeypatch.setattr(smtp_relay, "ist_relay_quelle",
                        lambda ip: gefragt.append(ip) or True)
    bauen, _, _ = listener
    smtp = main._LenientSMTP.__new__(main._LenientSMTP)
    smtp.require_starttls = False
    smtp.session = _Sitzung()
    monkeypatch.setattr(main._BaseSMTP, "connection_made", lambda self, t: None)
    smtp.connection_made(_Transport("10.1.5.7"))
    assert smtp.require_starttls is False
    assert gefragt == [], "ohne Pflicht darf die Geräteliste nicht befragt werden"


def test_ein_fehler_in_der_pruefung_behaelt_die_pflicht(listener, monkeypatch):
    """⚠️ Die Ausfallrichtung: Im Zweifel bleibt STARTTLS Pflicht.

    Ein Fehler beim Nachsehen (defekte Datenbank, fehlendes Modul) darf nicht
    dazu führen, dass die Verschlüsselung entfällt.
    """
    import smtp_relay
    monkeypatch.setattr(smtp_relay, "ist_relay_quelle",
                        lambda ip: (_ for _ in ()).throw(RuntimeError("kaputt")))
    bauen, _, _ = listener
    assert bauen("10.1.5.7").require_starttls is True


def test_tls_zustand_wird_je_geraet_festgehalten(listener):
    """⚠️ Ohne diese Anzeige wäre die Lockerung unsichtbar.

    Nach dem Wegfall der Pflicht ist die Frage „wer liefert im Klartext" nicht
    mehr aus dem Protokoll zu beantworten — sie gehört in die Übersicht, sonst
    fällt niemandem auf, dass ein Gerät seit Monaten unverschlüsselt sendet.
    """
    _, _, relay_hosts = listener
    assert relay_hosts.liste()[0]["tls"] == "unbekannt", "noch nie geliefert"

    relay_hosts.merke_zustellung("10.1.5.7", tls=False)
    assert relay_hosts.liste()[0]["tls"] == "nein"

    relay_hosts.merke_zustellung("10.1.5.7", tls=True)
    assert relay_hosts.liste()[0]["tls"] == "ja", (
        "Ein Gerät, das nachgerüstet wurde, muss das auch zeigen — sonst bleibt "
        "der Befund stehen, obwohl er behoben ist.")
