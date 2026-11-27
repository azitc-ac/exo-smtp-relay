"""Testmail Ende zu Ende: echter Listener, echter Rückweg (smarthost) mit STARTTLS zu einem
Exchange-Ersatz, der wie Exchange mit „250 2.6.0 <…> Queued mail for delivery“ antwortet.

Geprüft wird, was eine Testmail beweisen soll: Sie geht DENSELBEN Weg wie Gerätepost
(Annahme, Grenzen nach den Regeln des gewählten Geräts, Rückweg), das Ergebnis bleibt mit
Message-ID und Antwort von Exchange stehen — und sie öffnet das Relay für niemanden sonst.
"""
import smtplib
import socket
import ssl
import sys
from pathlib import Path

import pytest
from aiosmtpd.controller import Controller

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

GERAET = "10.99.0.7"
EXO_ANTWORT = "2.6.0 <abc@relay> [InternalId=123, Hostname=AM0PR01MB0000] Queued mail for delivery"


def _freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Exchange:
    def __init__(self):
        self.post = []

    async def handle_DATA(self, server, session, envelope):
        self.post.append((envelope.mail_from, list(envelope.rcpt_tos), envelope.content,
                          bool(getattr(session, "ssl", None))))
        return "250 " + EXO_ANTWORT


@pytest.fixture
def dienst(einstellungen, monkeypatch, tmp_path):
    import config
    import exo_mailboxes
    import mail_audit
    import main
    import relay_hosts
    import smarthost
    import testmail
    import tls_cert

    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    tls_cert.selbstsigniert_erzeugen("relay.firma.de")
    monkeypatch.setattr(relay_hosts, "DB_PATH", tmp_path / "relay_hosts.db")
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    mail_audit.init_db()
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda: {"chefin@firma.de", "scanner@firma.de"})
    monkeypatch.setattr(testmail, "_offen", {})

    exo = _Exchange()
    exo_port = _freier_port()
    exo_ctl = Controller(exo, hostname="127.0.0.1", port=exo_port, tls_context=main._build_tls_context())
    exo_ctl.start()
    einstellungen.update({"EXO_SMARTHOST": "127.0.0.1", "EXO_PORT": exo_port})
    roh = ssl.create_default_context()
    roh.check_hostname = False
    roh.verify_mode = ssl.CERT_NONE
    monkeypatch.setattr(smarthost, "_client_tls_context", lambda: roh)

    relay_port = _freier_port()
    monkeypatch.setattr(config, "SMTP_PORT", relay_port)
    monkeypatch.setattr(config, "SMTP_BIND", "0.0.0.0")
    from handler import RelayHandler
    relay_ctl = main._LenientController(RelayHandler(), hostname="127.0.0.1", port=relay_port,
                                        tls_context=main._build_tls_context(), require_starttls=True)
    relay_ctl.start()
    relay_hosts.speichern(GERAET, kommentar="Scanner")
    try:
        yield {"exo": exo, "werte": einstellungen, "relay_hosts": relay_hosts, "port": relay_port}
    finally:
        relay_ctl.stop()
        exo_ctl.stop()


def test_testmail_geht_den_ganzen_weg_und_das_ergebnis_bleibt(dienst):
    import mail_audit
    import testmail
    lauf = testmail.senden("scanner@firma.de", "chefin@firma.de", GERAET)
    assert lauf["ok"], lauf
    assert lauf["antwort"] == "250 " + EXO_ANTWORT, "Antwort von Exchange auf DATA fehlt"
    assert dienst["werte"]["TESTMAIL_LAUF"] == lauf, "Ergebnis nicht gespeichert"
    (von, an, inhalt, tls), = dienst["exo"].post
    assert von == "scanner@firma.de" and an == ["chefin@firma.de"] and tls, "Rückweg ohne STARTTLS"
    assert inhalt.startswith(b"Received: from [127.0.0.1]"), "nicht über den Listener gelaufen"
    assert lauf["mid"].encode() in inhalt
    # Der Server selbst wird nicht zum Gerät, und die Zähler des Geräts bleiben unberührt.
    assert dienst["relay_hosts"].host("127.0.0.1") is None
    assert not dienst["relay_hosts"].host(GERAET).get("letzte_mail")
    zeilen = mail_audit.query_events(quelle=f"Testmail ({GERAET})")
    assert [z["action"] for z in zeilen] == ["relay"], zeilen


def test_testmail_gilt_den_regeln_des_geraets(dienst):
    import testmail
    lauf = testmail.senden("scanner@firma.de", "jemand@extern.example", GERAET)
    assert not lauf["ok"] and "ausserhalb des Tenants" in lauf["fehler"], lauf
    assert lauf["antwort_listener"].startswith("550"), lauf
    assert dienst["exo"].post == []
    dienst["relay_hosts"].speichern(GERAET, kommentar="Scanner", extern=True)
    assert testmail.senden("scanner@firma.de", "jemand@extern.example", GERAET)["ok"]


def test_kennung_gilt_einmal_und_nur_fuer_diesen_prozess(dienst):
    """Wer die Testmail (samt Kopf) noch einmal einliefert oder einen eigenen Kopf erfindet,
    wird wie jedes unbekannte Gerät abgewiesen."""
    import testmail
    assert testmail.senden("scanner@firma.de", "chefin@firma.de", GERAET)["ok"]
    (_, _, inhalt, _), = dienst["exo"].post
    nochmal = inhalt.split(b"\r\n", 1)[1]                      # ohne die Received-Zeile des Relays
    assert testmail.KOPF.encode() in nochmal
    erfunden = nochmal.replace(testmail.KOPF.encode() + b": ", testmail.KOPF.encode() + b": x")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for daten in (nochmal, erfunden):
        with smtplib.SMTP("127.0.0.1", dienst["port"], timeout=10) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            with pytest.raises(smtplib.SMTPDataError) as e:
                s.sendmail("scanner@firma.de", ["chefin@firma.de"], daten)
            assert e.value.smtp_code == 554
    assert len(dienst["exo"].post) == 1


def test_ohne_freigegebenes_geraet_keine_testmail(dienst):
    import testmail
    erg = testmail.senden("scanner@firma.de", "chefin@firma.de", "10.99.0.250")
    assert not erg["ok"] and "freigegebenes Gerät" in erg["fehler"]
    dienst["relay_hosts"].speichern(GERAET, gesperrt=True)
    erg = testmail.senden("scanner@firma.de", "chefin@firma.de", GERAET)
    assert not erg["ok"] and "freigegebenes Gerät" in erg["fehler"], "gesperrtes Gerät gilt nicht"
    assert dienst["exo"].post == []


def test_rueckweg_meldet_die_antwort_von_exchange_auch_fuer_geraete(dienst):
    """Nebenbei: smarthost.send liefert die DATA-Antwort für JEDE Zustellung (Protokoll),
    nicht nur für die Testmail."""
    import smarthost
    erg = smarthost.send("scanner@firma.de", ["chefin@firma.de"], b"Subject: x\r\n\r\ny\r\n")
    assert erg == {"ziel": f"127.0.0.1:{dienst['werte']['EXO_PORT']}", "antwort": "250 " + EXO_ANTWORT}


def test_kennung_gilt_nur_von_einer_eigenen_adresse(monkeypatch):
    """Selbst mit gültiger Kennung: Von einer fremden Adresse ist es keine Testmail —
    und die Kennung bleibt für den echten Versand erhalten."""
    import testmail
    monkeypatch.setattr(testmail, "_offen", {"k-1": {"geraet": GERAET, "bis": 1e18}})
    roh = b"From: a@firma.de\r\n" + testmail.KOPF.encode() + b": k-1\r\n\r\nx\r\n"
    assert testmail.einloesen(roh, "10.99.0.9") is None
    assert testmail.einloesen(roh, "127.0.0.1") == {"geraet": GERAET, "bis": 1e18}
    assert testmail.einloesen(roh, "127.0.0.1") is None, "einmalig"
