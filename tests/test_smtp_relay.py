"""Das Relay hat drei Grenzen — und keine davon darf still ausfallen.

ANLASS (2026-08-25)
-------------------
Wiederkehrende Kundenanforderung: Drucker und Anwendungen liefern anonym per
SMTP ab, wie bei einem Exchange vor Ort.

⚠️ Der Ausgangspunkt war nicht „das kann das Gateway noch nicht", sondern das
Gegenteil: `handler.py` reicht jede Nachricht weiter, deren Absender nicht in
`MAILBOX_CONFIG` steht. Wer ein Netz in die Quell-IP-Liste einträgt, hat damit
ein Relay — ohne Absenderprüfung, ohne Zielbeschränkung, ohne dass es irgendwo
stünde. Dieses Modul macht daraus eine Entscheidung mit Grenzen.

Die Tests prüfen deshalb vor allem, dass die Grenzen HALTEN — nicht, dass das
Relay funktioniert. Ein Relay, das zu viel durchlässt, macht dem Kunden Ärger,
den er nicht uns zuschreibt, sondern seinem Ruf.
"""
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import smtp_relay  # noqa: E402


@pytest.fixture
def anlage(monkeypatch, tmp_path):
    """Ein Gateway mit einem eingetragenen Gerät und zwei bekannten Postfächern.

    Die Geräteliste ist eine echte Datenbank in einem Wegwerfverzeichnis —
    keine Attrappe. Sonst prüfte der Test seine eigene Vorstellung davon, wie
    `relay_hosts` sich verhält, statt das Modul selbst.
    """
    werte = {
        "SMTP_RELAY_ENABLED": True,
        "SMTP_RELAY_LERN_NETZE": [],
        "SMTP_RELAY_LERN_BIS": "",
        "SMTP_RELAY_EXTERN_VORGABE": False,
        "TENANT_DOMAIN": "firma.onmicrosoft.com",
    }
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))

    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "known_addresses",
                        lambda: {"chefin@firma.de", "lager@firma.de"})

    import relay_hosts
    monkeypatch.setattr(relay_hosts, "DB_PATH", tmp_path / "relay_hosts.db")
    relay_hosts.speichern("10.1.5.30", kommentar="Kopierer Empfang")
    return werte


def _lernfenster(stunden=1):
    """Ein Zeitpunkt in *stunden* Stunden, wie ihn die Oberfläche schreibt."""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=stunden)).isoformat()


def test_eingetragenes_geraet_darf_intern_zustellen(anlage):
    assert smtp_relay.ist_relay_quelle("10.1.5.30")
    erlaubt, grund, _ = smtp_relay.pruefe("drucker@firma.de", ["chefin@firma.de"], "10.1.5.30")
    assert erlaubt, grund


def test_nicht_eingetragene_adresse_ist_keine_relay_quelle(anlage):
    assert not smtp_relay.ist_relay_quelle("10.1.5.99"), (
        "Nachbar im selben Netz, aber nicht eingetragen — das Netz allein ist "
        "keine Freigabe mehr.")
    assert not smtp_relay.ist_relay_quelle("192.168.0.5")
    assert not smtp_relay.ist_relay_quelle("")
    assert not smtp_relay.ist_relay_quelle("keine-ip")


def test_abgeschaltet_ist_abgeschaltet(anlage):
    anlage["SMTP_RELAY_ENABLED"] = False
    assert not smtp_relay.ist_relay_quelle("10.1.5.30"), (
        "Bei abgeschaltetem Relay darf kein Netz als Quelle gelten — sonst "
        "wäre der Schalter wirkungslos.")


def test_fremde_absenderdomaene_wird_abgewiesen(anlage):
    """Ein übernommenes Gerät soll nicht als fremde Firma versenden können."""
    erlaubt, grund, antwort = smtp_relay.pruefe(
        "rechnung@paypal.com", ["chefin@firma.de"], "10.1.5.30")
    assert not erlaubt
    assert "paypal.com" in grund
    assert antwort.startswith("550"), "dauerhafte Ablehnung, kein Wiederversuch"


def test_leerer_absender_wird_abgewiesen(anlage):
    """Ein leerer Absender ist bei Zustellberichten üblich — über ein Relay
    hat er nichts zu suchen, denn er lässt sich keiner Domäne zuordnen."""
    erlaubt, _, _ = smtp_relay.pruefe("", ["chefin@firma.de"], "10.1.5.30")
    assert not erlaubt


def test_externes_ziel_nur_nach_freigabe(anlage):
    erlaubt, grund, antwort = smtp_relay.pruefe(
        "drucker@firma.de", ["kunde@extern.de"], "10.1.5.30")
    assert not erlaubt
    assert "kunde@extern.de" in grund
    assert antwort.startswith("550")

    import relay_hosts
    relay_hosts.speichern("10.1.5.30", extern=True)
    erlaubt, grund, _ = smtp_relay.pruefe(
        "drucker@firma.de", ["kunde@extern.de"], "10.1.5.30")
    assert erlaubt, grund


def test_unbekannte_adresse_eigener_domaene_zaehlt_nicht_als_intern(anlage):
    """⚠️ Feinheit mit Aussenwirkung: `gibtsnicht@firma.de` hat die richtige
    Domäne, aber kein Postfach. Exchange erzeugte daraus einen
    Unzustellbarkeitsbericht — der nach aussen geht. Damit wäre die
    Zielbeschränkung umgangen.
    """
    erlaubt, _, _ = smtp_relay.pruefe(
        "drucker@firma.de", ["gibtsnicht@firma.de"], "10.1.5.30")
    assert not erlaubt


def test_ohne_bekannte_adressen_wird_verweigert(anlage, monkeypatch):
    """⚠️ Die Ausfallrichtung — der wichtigste Test hier.

    `smtp_acl` lässt bei leerer Liste alles durch, damit der Mailfluss nicht
    stoppt. Für ein Relay wäre das falsch herum: Ohne die eigenen Adressen
    lässt sich weder Absender noch Ziel beurteilen. Dann gilt: nichts.
    """
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "known_addresses", set)
    erlaubt, grund, antwort = smtp_relay.pruefe(
        "drucker@firma.de", ["chefin@firma.de"], "10.1.5.30")
    assert not erlaubt
    assert "nicht bekannt" in grund
    assert antwort.startswith("451"), (
        "vorübergehend, nicht dauerhaft — der Zustand behebt sich, sobald die "
        "Postfachliste geladen ist, und das Gerät soll es dann erneut versuchen")


def test_kaputte_netzangabe_oeffnet_nichts(anlage):
    anlage["SMTP_RELAY_LERN_NETZE"] = ["nonsens", "10.1.5.0/24"]
    anlage["SMTP_RELAY_LERN_BIS"] = _lernfenster()
    assert smtp_relay.ist_relay_quelle("10.1.5.44"), "gültige Einträge gelten weiter"
    assert not smtp_relay.ist_relay_quelle("8.8.8.8"), (
        "Ein unlesbarer Eintrag darf nicht dazu führen, dass alles erlaubt ist.")


def test_nur_im_smtp_modus(anlage):
    """Der Rückweg entscheidet — Graph und IMAP können fremde Absender nicht.

    ⚠️ Diese Grenze steht bewusst NICHT nur in der Oberfläche: Der Modus lässt
    sich nachträglich umstellen, das Relay bliebe eingeschaltet und nähme Post
    an, die anschliessend niemand zustellen kann. Angenommen und dann verworfen
    ist der schlechteste aller Ausgänge — das Gerät meldet Erfolg.
    """
    gut = ("drucker@firma.de", ["chefin@firma.de"], "10.1.5.30")
    assert smtp_relay.pruefe(*gut)[0], "im Modus smtp muss es gehen"

    for modus in ("graph", "imap", "smtp587"):
        anlage["REINJECT_MODE"] = modus
        erlaubt, grund, antwort = smtp_relay.pruefe(*gut)
        assert not erlaubt, f"Modus {modus} hätte abgelehnt werden müssen"
        assert modus in grund, "der Grund muss den Modus nennen"
        # 4xx, nicht 5xx: Das Gerät soll es nach einer Umstellung erneut
        # versuchen — die Ursache ist Konfiguration, kein dauerhafter Fehler.
        assert antwort.startswith("451"), antwort


# ── Lernmodus ────────────────────────────────────────────────────────────────
#
# Der Lernmodus ist die Stelle, an der aus „darf niemand" ein Eintrag wird. Er
# ist damit der einzige Weg, auf dem sich die Freigabe von selbst erweitert —
# und deshalb der Teil, der am genauesten begrenzt sein muss.

def test_lernnetz_ohne_zeitfenster_laesst_nichts_durch(anlage):
    """⚠️ Der Kern des Umbaus.

    Ein Netz einzutragen darf für sich genommen NICHTS bewirken. Täte es das,
    wäre der Unterschied zur alten Netzliste nur ein anderer Name — und die
    Geräteliste keine verlässliche Aussage mehr darüber, wer einliefern darf.
    """
    anlage["SMTP_RELAY_LERN_NETZE"] = ["10.1.5.0/24"]
    anlage["SMTP_RELAY_LERN_BIS"] = ""
    assert not smtp_relay.ist_relay_quelle("10.1.5.44")


def test_abgelaufenes_lernfenster_laesst_nichts_durch(anlage):
    anlage["SMTP_RELAY_LERN_NETZE"] = ["10.1.5.0/24"]
    anlage["SMTP_RELAY_LERN_BIS"] = _lernfenster(-1)     # vor einer Stunde vorbei
    assert smtp_relay.lernmodus_bis() is None
    assert not smtp_relay.ist_relay_quelle("10.1.5.44")


def test_im_lernfenster_wird_das_geraet_aufgenommen(anlage):
    """Nach dem Lernlauf steht das Gerät drin — und darf weiter, wenn das
    Fenster längst zu ist. Genau dafür ist der Modus da."""
    import relay_hosts
    anlage["SMTP_RELAY_LERN_NETZE"] = ["10.1.5.0/24"]
    anlage["SMTP_RELAY_LERN_BIS"] = _lernfenster()

    assert smtp_relay.ist_relay_quelle("10.1.5.44")
    erlaubt, grund, _ = smtp_relay.pruefe(
        "scanner@firma.de", ["lager@firma.de"], "10.1.5.44")
    assert erlaubt, grund

    eintrag = relay_hosts.host("10.1.5.44")
    assert eintrag and eintrag["gelernt"] == 1
    assert eintrag["extern"] == 0, "gelernte Geräte dürfen zunächst nur intern"

    anlage["SMTP_RELAY_LERN_BIS"] = ""
    assert smtp_relay.ist_relay_quelle("10.1.5.44"), (
        "Nach dem Lernlauf muss das Gerät ohne Fenster weiterlaufen — sonst "
        "bräche der Mailfluss genau dann ab, wenn niemand mehr hinsieht.")


def test_abgewiesenes_geraet_wird_nicht_gelernt(anlage):
    """⚠️ Gelernt wird beim ZUSTELLEN, nicht beim Verbinden.

    Ein Gerät mit fremder Absenderdomäne wird abgewiesen — und darf dadurch
    nicht in die Liste geraten. Sonst füllte ein Absender, der ohnehin nichts
    darf, die Übersicht mit Einträgen, die der Admin dann durchsieht.
    """
    import relay_hosts
    anlage["SMTP_RELAY_LERN_NETZE"] = ["10.1.5.0/24"]
    anlage["SMTP_RELAY_LERN_BIS"] = _lernfenster()

    erlaubt, _, _ = smtp_relay.pruefe(
        "spam@fremd.example", ["chefin@firma.de"], "10.1.5.55")
    assert not erlaubt
    assert relay_hosts.host("10.1.5.55") is None, (
        "abgewiesene Absender dürfen die Geräteliste nicht füllen")


def test_lernen_folgt_der_vorgabe_fuer_externe_ziele(anlage):
    import relay_hosts
    anlage["SMTP_RELAY_LERN_NETZE"] = ["10.1.5.0/24"]
    anlage["SMTP_RELAY_LERN_BIS"] = _lernfenster()
    anlage["SMTP_RELAY_EXTERN_VORGABE"] = True

    erlaubt, grund, _ = smtp_relay.pruefe(
        "scanner@firma.de", ["kunde@extern.example"], "10.1.5.44")
    assert erlaubt, grund
    assert relay_hosts.host("10.1.5.44")["extern"] == 1


def test_kaputter_zeitpunkt_schaltet_nicht_ein(anlage):
    """Ein unlesbarer Zeitpunkt gilt als AUS, nicht als unbegrenzt."""
    anlage["SMTP_RELAY_LERN_NETZE"] = ["10.1.5.0/24"]
    for murks in ("morgen", "2026-13-45", "999999999999"):
        anlage["SMTP_RELAY_LERN_BIS"] = murks
        assert smtp_relay.lernmodus_bis() is None, murks
        assert not smtp_relay.ist_relay_quelle("10.1.5.44"), murks


# ── Sperren und Abweisungsliste ──────────────────────────────────────────────

def test_gesperrtes_geraet_kommt_nicht_durch(anlage):
    import relay_hosts
    relay_hosts.speichern("10.1.5.30", gesperrt=True)
    assert not smtp_relay.ist_relay_quelle("10.1.5.30")


def test_sperre_schlaegt_den_lernmodus(anlage):
    """⚠️ Der gefährlichste Weg, das falsch zu bauen.

    Ein gesperrtes Gerät steht in der Liste UND liegt im Lernnetz. Wer die
    Sperre nur als „nicht erlaubt" behandelt und danach weiter zum Lernmodus
    fällt, gibt es beim nächsten Lernlauf wieder frei — und die Sperre wäre
    eine Empfehlung. Sie muss abschliessend sein.
    """
    import relay_hosts
    relay_hosts.speichern("10.1.5.30", gesperrt=True)
    anlage["SMTP_RELAY_LERN_NETZE"] = ["10.1.5.0/24"]
    anlage["SMTP_RELAY_LERN_BIS"] = _lernfenster()

    assert not smtp_relay.ist_relay_quelle("10.1.5.30"), (
        "Der Lernmodus darf eine Sperre nicht aufheben.")
    assert smtp_relay.ist_relay_quelle("10.1.5.44"), (
        "Gegenprobe: der ungesperrte Nachbar wird sehr wohl gelernt — sonst "
        "prüfte der Test nur, dass der Lernmodus überhaupt nicht greift.")


def test_entsperren_stellt_wieder_her(anlage):
    import relay_hosts
    relay_hosts.speichern("10.1.5.30", gesperrt=True)
    assert not smtp_relay.ist_relay_quelle("10.1.5.30")
    relay_hosts.speichern("10.1.5.30", gesperrt=False)
    assert smtp_relay.ist_relay_quelle("10.1.5.30")


def test_sperren_laesst_kommentar_stehen(anlage):
    """`None` heisst unverändert — sonst räumte das Sperren die Beschreibung ab."""
    import relay_hosts
    relay_hosts.speichern("10.1.5.30", kommentar="Kopierer Empfang",
                          ansprechpartner="Frau Meier")
    relay_hosts.speichern("10.1.5.30", gesperrt=True)
    eintrag = relay_hosts.host("10.1.5.30")
    assert eintrag["kommentar"] == "Kopierer Empfang"
    assert eintrag["ansprechpartner"] == "Frau Meier"


def test_abweisungen_werden_gesammelt_und_gezaehlt(anlage):
    import relay_hosts
    for _ in range(3):
        relay_hosts.merke_abweisung("10.1.5.77", "neu@firma.de",
                                    ["chefin@firma.de"], "nicht in der Geräteliste")
    liste = relay_hosts.abgewiesene()
    assert len(liste) == 1, "eine Zeile je Adresse, kein Verlauf"
    assert liste[0]["anzahl"] == 3
    assert liste[0]["absender"] == "neu@firma.de"
    assert liste[0]["erstmals"] and liste[0]["zuletzt"]


def test_uebernommenes_geraet_verschwindet_aus_der_abweisungsliste(anlage):
    import relay_hosts
    relay_hosts.merke_abweisung("10.1.5.77", "neu@firma.de", ["chefin@firma.de"], "x")
    relay_hosts.speichern("10.1.5.77", kommentar="Neuer Scanner")
    relay_hosts.vergiss_abweisung("10.1.5.77")
    assert relay_hosts.abgewiesene() == []
    assert smtp_relay.ist_relay_quelle("10.1.5.77")


# ── Lernbereiche: Netz ODER Spanne ───────────────────────────────────────────

@pytest.mark.parametrize("bereich,drin,draussen", [
    ("192.168.1.0/24",              "192.168.1.200", "192.168.2.1"),
    ("172.16.16.10-172.16.17.20",   "172.16.16.255", "172.16.17.21"),
    ("172.16.16.10-172.16.17.20",   "172.16.17.20",  "172.16.16.9"),
    ("10.0.0.5-10.0.0.5",           "10.0.0.5",      "10.0.0.6"),
    # Verdreht eingegeben — soll trotzdem gelten, statt still nichts zu tun.
    ("10.0.0.20-10.0.0.10",         "10.0.0.15",     "10.0.0.21"),
])
def test_lernbereich_versteht_netz_und_spanne(anlage, bereich, drin, draussen):
    """Eine Spanne über Netzgrenzen hinweg lässt sich nicht als `/nn` schreiben.

    Die Ränder sind ausdrücklich mitgeprüft: Bei `172.16.16.10-172.16.17.20`
    liegt `172.16.16.255` mittendrin, obwohl es das Ende eines `/24` ist — wer
    die Spanne intern in Netze zerlegt, verliert genau solche Adressen.
    """
    anlage["SMTP_RELAY_LERN_NETZE"] = [bereich]
    anlage["SMTP_RELAY_LERN_BIS"] = _lernfenster()
    assert smtp_relay.ist_relay_quelle(drin), f"{drin} sollte in {bereich} liegen"
    assert not smtp_relay.ist_relay_quelle(draussen), \
        f"{draussen} liegt ausserhalb von {bereich}"


def test_unsinnige_spanne_oeffnet_nichts(anlage):
    anlage["SMTP_RELAY_LERN_BIS"] = _lernfenster()
    for murks in ("10.0.0.1-", "-10.0.0.9", "10.0.0.1-nonsens",
                  "10.0.0.1-::5", "a-b"):
        anlage["SMTP_RELAY_LERN_NETZE"] = [murks]
        assert not smtp_relay.ist_relay_quelle("10.0.0.1"), murks
        assert not smtp_relay.ist_relay_quelle("10.0.0.5"), murks


def test_lerndauer_wird_gedeckelt(anlage):
    """⚠️ Die Höchstdauer gilt beim LESEN, nicht nur im Formular.

    Ein Zeitpunkt in ferner Zukunft — von Hand eingetragen oder aus einer alten
    Sicherung — ergäbe sonst ein dauerhaft lernendes Gateway. Also genau das
    offene Relay, dessen Vermeidung der Zweck der ganzen Konstruktion ist.
    """
    from datetime import datetime, timedelta, timezone
    anlage["SMTP_RELAY_LERN_NETZE"] = ["10.1.5.0/24"]
    anlage["SMTP_RELAY_LERN_BIS"] = _lernfenster(24 * 365)      # ein Jahr

    bis = smtp_relay.lernmodus_bis()
    assert bis is not None, "gedeckelt heisst nicht abgeschaltet"
    rest = (bis - datetime.now(timezone.utc)).total_seconds() / 60
    assert rest <= smtp_relay.MAX_LERNDAUER_MIN + 1, f"{rest:.0f} Minuten"
    assert rest > smtp_relay.MAX_LERNDAUER_MIN - 1, f"{rest:.0f} Minuten"


def test_kuerzere_dauer_bleibt_unangetastet(anlage):
    """Gegenprobe: Der Deckel darf nicht jede Angabe auf 120 Minuten strecken."""
    from datetime import datetime, timezone
    anlage["SMTP_RELAY_LERN_BIS"] = _lernfenster(0.25)          # 15 Minuten
    bis = smtp_relay.lernmodus_bis()
    rest = (bis - datetime.now(timezone.utc)).total_seconds() / 60
    assert 14 < rest <= 15, f"{rest:.1f} Minuten"


def test_standarddauer_liegt_unter_der_hoechstdauer(anlage):
    assert 0 < smtp_relay.STANDARD_LERNDAUER_MIN < smtp_relay.MAX_LERNDAUER_MIN
