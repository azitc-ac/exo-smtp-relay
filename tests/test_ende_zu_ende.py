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


def test_unbekanntes_geraet_muss_starttls_und_wird_dann_abgewiesen(dienst):
    """Für eine fremde Adresse bleibt die Pflicht; nach STARTTLS scheitert sie
    an der Geräteliste — auf keinem der beiden Wege kommt Post durch."""
    import ssl
    with smtplib.SMTP("127.0.0.1", dienst["port"], timeout=10) as s:
        s.ehlo()
        with pytest.raises(smtplib.SMTPResponseException) as e:
            s.sendmail("drucker@firma.de", ["chefin@firma.de"], NACHRICHT)
        assert e.value.smtp_code == 530, "ohne STARTTLS: 530 Must issue a STARTTLS command first"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with smtplib.SMTP("127.0.0.1", dienst["port"], timeout=10) as s:
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
        with pytest.raises(smtplib.SMTPDataError) as e:
            s.sendmail("drucker@firma.de", ["chefin@firma.de"], NACHRICHT)
        assert e.value.smtp_code == 554
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
