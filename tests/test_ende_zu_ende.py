"""Ende zu Ende: ein echter Listener, ein echter Smarthost-Ersatz, echtes SMTP.

Die Einheitentests prüfen die Entscheidung; dieser Test prüft, dass sie im
laufenden Dienst auch getroffen wird — mit aiosmtpd, STARTTLS-Pflicht und
smtplib, so wie ein Drucker es täte. Ports werden vom Betriebssystem vergeben.
"""
import smtplib
import socket
import sys
from pathlib import Path

import pytest
from aiosmtpd.controller import Controller

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


def _freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Senke:
    """Der Smarthost-Ersatz: nimmt alles an und merkt es sich."""
    def __init__(self):
        self.post = []

    async def handle_DATA(self, server, session, envelope):
        self.post.append((envelope.mail_from, list(envelope.rcpt_tos), envelope.content))
        return "250 OK"


@pytest.fixture
def dienst(einstellungen, monkeypatch, tmp_path):
    import config
    import exo_mailboxes
    import relay_hosts
    import tls_cert
    import mail_audit
    import main
    import smarthost

    monkeypatch.setattr(config, "SMTP_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(config, "SMTP_TLS_KEY", str(tmp_path / "key.pem"))
    tls_cert.selbstsigniert_erzeugen("relay.firma.de")
    monkeypatch.setattr(relay_hosts, "DB_PATH", tmp_path / "relay_hosts.db")
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    mail_audit.init_db()
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda: {"chefin@firma.de", "lager@firma.de"})

    senke = _Senke()
    senke_port = _freier_port()
    senke_ctl = Controller(senke, hostname="127.0.0.1", port=senke_port)
    senke_ctl.start()
    # Der Smarthost-Ersatz spricht kein STARTTLS — direkt einliefern.
    def _send(mail_from, rcpt_tos, content):
        with smtplib.SMTP("127.0.0.1", senke_port, timeout=10) as s:
            s.sendmail(mail_from, rcpt_tos, content)
    monkeypatch.setattr(smarthost, "send", _send)

    relay_port = _freier_port()
    tls_ctx = main._build_tls_context()
    from handler import RelayHandler
    relay_ctl = main._LenientController(RelayHandler(), hostname="127.0.0.1", port=relay_port,
                                        tls_context=tls_ctx, require_starttls=True)
    relay_ctl.start()
    try:
        yield {"port": relay_port, "senke": senke, "werte": einstellungen, "relay_hosts": relay_hosts}
    finally:
        relay_ctl.stop()
        senke_ctl.stop()


NACHRICHT = b"From: drucker@firma.de\r\nTo: chefin@firma.de\r\nSubject: Scan\r\n\r\nAnbei.\r\n"


def test_eingetragenes_geraet_liefert_ohne_starttls_ein(dienst):
    dienst["relay_hosts"].speichern("127.0.0.1", kommentar="Testdrucker")
    with smtplib.SMTP("127.0.0.1", dienst["port"], timeout=10) as s:
        s.ehlo()
        assert s.has_extn("starttls"), "STARTTLS wird weiterhin ANGEBOTEN"
        s.sendmail("drucker@firma.de", ["chefin@firma.de"], NACHRICHT)
    assert len(dienst["senke"].post) == 1
    von, an, inhalt = dienst["senke"].post[0]
    assert von == "drucker@firma.de" and an == ["chefin@firma.de"]
    assert inhalt.startswith(b"Received: from [127.0.0.1]")
    assert b"Subject: Scan" in inhalt


def test_unbekanntes_geraet_wird_bei_rcpt_mit_grund_abgewiesen(dienst, monkeypatch):
    """Bis 0.2.61: ohne STARTTLS „530 Must issue a STARTTLS command first“, danach
    „554 5.7.1 Access denied“ — wer das las, suchte den Fehler am Zertifikat. Jetzt
    schon bei RCPT, ohne STARTTLS, mit Adresse und Weg zur Freigabe; nichts kommt durch.
    Als „fremde“ Adresse dient 127.0.0.1, indem die eigenen Adressen umgebogen werden."""
    import testmail
    monkeypatch.setattr(testmail, "eigene_quellen", lambda: {"10.99.0.250"})
    with smtplib.SMTP("127.0.0.1", dienst["port"], timeout=10) as s:
        s.ehlo()
        with pytest.raises(smtplib.SMTPRecipientsRefused) as e:
            s.sendmail("drucker@firma.de", ["chefin@firma.de", "lager@firma.de"], NACHRICHT)
    assert set(e.value.recipients) == {"chefin@firma.de", "lager@firma.de"}
    code, text = e.value.recipients["chefin@firma.de"]
    assert code == 554, (code, text)
    assert b"127.0.0.1 nicht freigegeben" in text and b"Schritt 5" in text, text
    assert dienst["senke"].post == []
    abgewiesen = dienst["relay_hosts"].abgewiesene()
    assert [(a["ip"], a["absender"], a["anzahl"]) for a in abgewiesen] == [("127.0.0.1", "drucker@firma.de", 1)]


def test_eigene_adresse_ohne_kennung_bleibt_bei_starttls_und_wird_abgewiesen(dienst):
    """Die eigene Adresse des Servers (Testmail-Weg) behält die STARTTLS-Pflicht und wird
    ohne gültige Einmal-Kennung bei DATA abgewiesen — der Server ist kein Gerät."""
    import ssl
    with smtplib.SMTP("127.0.0.1", dienst["port"], timeout=10) as s:
        s.ehlo()
        with pytest.raises(smtplib.SMTPResponseException) as e:
            s.sendmail("drucker@firma.de", ["chefin@firma.de"], NACHRICHT)
        assert e.value.smtp_code == 530
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with smtplib.SMTP("127.0.0.1", dienst["port"], timeout=10) as s:
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
        with pytest.raises(smtplib.SMTPDataError) as e:
            s.sendmail("drucker@firma.de", ["chefin@firma.de"], NACHRICHT)
        assert e.value.smtp_code == 554 and b"nicht freigegeben" in e.value.smtp_error
    assert dienst["senke"].post == []


def test_fremder_absender_wird_mit_550_abgelehnt(dienst):
    dienst["relay_hosts"].speichern("127.0.0.1")
    with smtplib.SMTP("127.0.0.1", dienst["port"], timeout=10) as s:
        with pytest.raises(smtplib.SMTPDataError) as e:
            s.sendmail("rechnung@paypal.com", ["chefin@firma.de"], NACHRICHT)
        assert e.value.smtp_code == 550
    assert dienst["senke"].post == []


def test_mail_from_mit_fremden_parametern_wird_angenommen(dienst):
    """Manche Geräte hängen `AUTH=<>` an MAIL FROM — das darf kein 555 geben."""
    dienst["relay_hosts"].speichern("127.0.0.1")
    with smtplib.SMTP("127.0.0.1", dienst["port"], timeout=10) as s:
        s.ehlo()
        code, _ = s.docmd("MAIL FROM:<drucker@firma.de> AUTH=<> XFOO=1")
        assert code == 250
        code, _ = s.docmd("RCPT TO:<chefin@firma.de>")
        assert code == 250
        s.rset()


def test_smtp_ascii():
    import main
    assert main.smtp_ascii("550 5.7.1 Absenderdomäne für das Relay — „nicht“ zulässig…") == \
        '550 5.7.1 Absenderdomaene fuer das Relay - "nicht" zulaessig...'
    assert main.smtp_ascii("451 Zeichen € bleibt erkennbar") == "451 Zeichen ? bleibt erkennbar"


def test_ablehnung_mit_umlaut_kommt_als_ascii_an(dienst):
    """Wie Gateway v1.12.24: Die Texte in smtp_relay.py (gespiegelt) tragen
    Umlaute. Ohne Umschreibung warf aiosmtpd (SMTPUTF8 aus) beim Senden, und
    das Gerät bekam „500 Error: (UnicodeEncodeError)“ statt der 550."""
    dienst["relay_hosts"].speichern("127.0.0.1")
    with smtplib.SMTP("127.0.0.1", dienst["port"], timeout=10) as s:
        with pytest.raises(smtplib.SMTPDataError) as e:
            s.sendmail("rechnung@paypal.com", ["chefin@firma.de"], NACHRICHT)
    assert e.value.smtp_code == 550, e.value
    assert e.value.smtp_error.decode("ascii") == "5.7.1 Absenderdomaene fuer das Relay nicht zulaessig"
