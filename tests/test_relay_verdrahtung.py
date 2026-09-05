"""Die Verdrahtung in `handler.RelayHandler` — es gibt keinen Weg an der Liste vorbei.

Übernommen aus dem Gateway (`tests/test_relay_verdrahtung.py`) und an den
eigenständigen Dienst angepasst: Dort umgeht der Relay-Zweig die Exchange-
Adressliste; hier IST er der einzige Weg. Die gefährliche Richtung ist
dieselbe: Ein Fehler, der zu viel durchlässt, ist still.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import handler  # noqa: E402


class _Sitzung:
    def __init__(self, ip, ssl=None):
        self.peer = (ip, 5000)
        self.ssl = ssl


class _Umschlag:
    def __init__(self, absender, empfaenger):
        self.mail_from = absender
        self.rcpt_tos = list(empfaenger)
        self.content = b"Subject: Scan\r\nMessage-ID: <1@x>\r\n\r\nAnbei.\r\n"


@pytest.fixture
def anlage(einstellungen, monkeypatch, tmp_path):
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda: {"chefin@firma.de"})
    import relay_hosts
    monkeypatch.setattr(relay_hosts, "DB_PATH", tmp_path / "relay_hosts.db")
    relay_hosts.speichern("10.1.5.30", kommentar="Kopierer Empfang")

    gesehen = {}
    import smarthost
    def _halt(mail_from, rcpt_tos, content):
        gesehen["zugestellt"] = (mail_from, rcpt_tos, content)
    monkeypatch.setattr(smarthost, "send", _halt)

    import mail_audit
    ereignisse = []
    monkeypatch.setattr(mail_audit, "log_event", lambda **kw: ereignisse.append(kw))
    einstellungen["_ereignisse"] = ereignisse
    einstellungen["_gesehen"] = gesehen
    return einstellungen


def _lauf(ip, absender="drucker@firma.de", empfaenger=("chefin@firma.de",), ssl=None):
    return asyncio.run(handler.RelayHandler().handle_DATA(
        None, _Sitzung(ip, ssl), _Umschlag(absender, empfaenger)))


def test_eingetragenes_geraet_kommt_durch(anlage):
    antwort = _lauf("10.1.5.30")
    assert antwort.startswith("250"), antwort
    assert anlage["_gesehen"], "die Nachricht kam nicht bis zur Zustellung"
    assert anlage["_ereignisse"][-1]["action"] == "relay"
    assert anlage["_ereignisse"][-1]["quelle"] == "10.1.5.30"


def test_fremdes_netz_wird_abgewiesen(anlage):
    assert _lauf("203.0.113.9") == "554 5.7.1 Access denied"
    assert not anlage["_gesehen"]


def test_nachbar_im_selben_netz_ist_keine_freigabe(anlage):
    assert _lauf("10.1.5.31") == "554 5.7.1 Access denied"


def test_abgeschaltetes_relay_weist_alles_ab(anlage):
    anlage["SMTP_RELAY_ENABLED"] = False
    assert _lauf("10.1.5.30") == "554 5.7.1 Access denied"
    assert not anlage["_gesehen"]


def test_gesperrtes_geraet_wird_im_handler_abgewiesen(anlage):
    import relay_hosts
    relay_hosts.speichern("10.1.5.30", gesperrt=True)
    assert _lauf("10.1.5.30") == "554 5.7.1 Access denied"
    assert not anlage["_gesehen"]


def test_unbekanntes_geraet_landet_in_der_abweisungsliste(anlage):
    import relay_hosts
    assert _lauf("10.1.5.99", absender="neu@firma.de") == "554 5.7.1 Access denied"
    offen = relay_hosts.abgewiesene()
    assert [z["ip"] for z in offen] == ["10.1.5.99"]
    assert offen[0]["absender"] == "neu@firma.de"
    assert "Geräteliste" in offen[0]["grund"]


def test_ohne_relay_wird_nichts_gesammelt(anlage):
    import relay_hosts
    anlage["SMTP_RELAY_ENABLED"] = False
    _lauf("203.0.113.9", absender="bot@fremd.example")
    assert relay_hosts.abgewiesene() == []


def test_fremde_absenderdomaene_wird_abgewiesen_und_protokolliert(anlage):
    antwort = _lauf("10.1.5.30", absender="werbung@fremd.example")
    assert antwort.startswith("550"), antwort
    assert not anlage["_gesehen"]
    ereignisse = anlage["_ereignisse"]
    assert len(ereignisse) == 1
    assert ereignisse[0]["action"] == "relay_abgelehnt"
    assert "fremd.example" in (ereignisse[0]["error"] or "")


def test_externes_ziel_ohne_freigabe_wird_abgewiesen(anlage):
    antwort = _lauf("10.1.5.30", empfaenger=("kunde@extern.example",))
    assert antwort.startswith("550"), antwort
    assert not anlage["_gesehen"]


def test_zustellfehler_wird_zum_wiederversuch(anlage, monkeypatch):
    """⚠️ Scheitert der Smarthost, ist die Post NICHT angenommen.

    Ein „250 OK" mit anschliessendem Verlust wäre der schlechteste Ausgang:
    Das Gerät meldet Erfolg, und niemand sucht. 4xx lässt es erneut versuchen.
    """
    import smarthost
    import relay_hosts
    def _kaputt(*a, **kw):
        raise ConnectionRefusedError("smarthost weg")
    monkeypatch.setattr(smarthost, "send", _kaputt)
    antwort = _lauf("10.1.5.30")
    assert antwort.startswith("451"), antwort
    assert anlage["_ereignisse"][-1]["action"] == "relay_fehler"
    assert relay_hosts.liste()[0]["zaehler"][30] == 0, (
        "nicht zugestellte Post darf nicht als Sendeaufkommen zählen")


def test_zustellung_wird_gezaehlt(anlage):
    import relay_hosts
    assert _lauf("10.1.5.30").startswith("250")
    assert _lauf("10.1.5.30").startswith("250")
    eintrag = relay_hosts.liste()[0]
    assert eintrag["ip"] == "10.1.5.30"
    assert eintrag["letzte_mail"]
    assert eintrag["zaehler"][30] == 2


def test_abgewiesene_post_wird_nicht_mitgezaehlt(anlage):
    import relay_hosts
    _lauf("10.1.5.30", absender="werbung@fremd.example")
    assert relay_hosts.liste()[0]["zaehler"][30] == 0


def test_der_gemessene_tls_zustand_landet_in_der_liste(anlage):
    import relay_hosts
    _lauf("10.1.5.30", ssl=None)
    assert relay_hosts.liste()[0]["tls"] == "nein"
    _lauf("10.1.5.30", ssl={"cipher": ("TLS_AES_256_GCM_SHA384",)})
    assert relay_hosts.liste()[0]["tls"] == "ja"


def test_received_kopfzeile_nennt_das_geraet(anlage):
    """Exchange hängt eigene Received-Zeilen an — diese hier ist die einzige,
    die das Gerät nennt. Ohne sie wäre im Kopf der Nachricht nicht mehr zu
    sehen, welcher Drucker sie geschickt hat."""
    _lauf("10.1.5.30", ssl={"cipher": ("x",)})
    _, _, inhalt = anlage["_gesehen"]["zugestellt"]
    kopf = inhalt.split(b"\r\n\r\n", 1)[0].decode()
    assert kopf.startswith("Received: from [10.1.5.30] by relay.firma.de")
    assert "with ESMTPS" in kopf
    assert b"Subject: Scan" in inhalt, "der Rest der Nachricht bleibt unverändert"


def test_ohne_bekannte_adressen_wird_vorlaeufig_abgewiesen(anlage, monkeypatch):
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "known_addresses", set)
    antwort = _lauf("10.1.5.30")
    assert antwort.startswith("451"), antwort
    assert not anlage["_gesehen"]
