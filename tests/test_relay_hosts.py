"""Die Geräteliste und ihre Zahlen.

ANLASS (2026-08-25)
-------------------
Die Übersicht zeigt je Gerät, wie viel es in den letzten 30/90/180/360 Tagen
eingeliefert hat. Solche Zahlen sind heikel: Sie sehen nach Messung aus, und
niemand rechnet nach. Eine Zahl, die zu hoch ist, führt zu der Annahme, ein
längst ausgetauschter Kopierer sei noch in Betrieb — und er bleibt in der
Freigabe stehen.

Deshalb prüfen die Tests hier die Fenstergrenzen mit gesetzten Tagen statt mit
gerade erzeugtem Verkehr: Nur so lässt sich zeigen, dass ein Ereignis von vor
95 Tagen im 90-Tage-Fenster NICHT mitzählt.
"""
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import relay_hosts  # noqa: E402


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(relay_hosts, "DB_PATH", tmp_path / "relay_hosts.db")
    return relay_hosts


def _eintragen(ip, tage_zurueck, anzahl):
    """Zähler für einen bestimmten Tag setzen — ohne die Uhr zu verbiegen."""
    from datetime import timedelta
    tag = (relay_hosts._jetzt() - timedelta(days=tage_zurueck)).strftime("%Y-%m-%d")
    with relay_hosts._conn() as c:
        c.execute("INSERT OR REPLACE INTO tage (ip, tag, anzahl) VALUES (?,?,?)",
                  (ip, tag, anzahl))


def test_fenster_grenzen_zaehlen_richtig(db):
    """⚠️ Der Test, der die Zahlen erst glaubwürdig macht.

    Je Fenster ein Ereignis knapp innerhalb und eines knapp ausserhalb. Wer
    `>=` und `>` verwechselt oder mit Monaten statt Tagen rechnet, fällt hier
    auf — im Betrieb dagegen erst, wenn jemand nachrechnet, und das tut
    niemand.
    """
    db.speichern("10.0.0.1")
    _eintragen("10.0.0.1", 0, 5)       # heute
    _eintragen("10.0.0.1", 29, 1)      # in allen Fenstern
    _eintragen("10.0.0.1", 60, 1)      # nicht in 30
    _eintragen("10.0.0.1", 120, 1)     # nicht in 30/90
    _eintragen("10.0.0.1", 200, 1)     # nur in 360
    _eintragen("10.0.0.1", 400, 99)    # in keinem — und deutlich zu gross,
                                       # damit ein Mitzählen sofort auffällt

    z = db.liste()[0]["zaehler"]
    assert z[30] == 6, z
    assert z[90] == 7, z
    assert z[180] == 8, z
    assert z[360] == 9, z


@pytest.mark.parametrize("fenster", relay_hosts.FENSTER)
def test_direkt_am_rand_des_fensters(db, fenster):
    """Genau auf der Kante — der einzige Ort, an dem `>=` und `>` sich trennen.

    Der Test darüber prüft die Fenster grob (29/60/120/200 Tage). Das erwischt
    eine Verwechslung von Tagen und Monaten, aber nicht die Kante selbst: Ein
    Ausdruck, der einen Tag zu kurz greift, käme dort ungeschoren durch.
    """
    db.speichern("10.0.0.99")
    _eintragen("10.0.0.99", fenster, 1)          # genau am Rand — zählt
    _eintragen("10.0.0.99", fenster + 1, 1)      # einen Tag zu alt — zählt nicht
    assert db.liste()[0]["zaehler"][fenster] == 1


def test_zaehler_summiert_mehrere_mails_am_selben_tag(db):
    db.speichern("10.0.0.2")
    for _ in range(4):
        db.merke_zustellung("10.0.0.2")
    z = db.liste()[0]
    assert z["zaehler"][30] == 4
    assert z["letzte_mail"], "der Zeitpunkt der letzten Mail muss mitlaufen"


def test_zaehler_ohne_eintrag_stuerzen_nicht_ab(db):
    """Ein Gerät ohne jede Mail zeigt Nullen, keine Lücke."""
    db.speichern("10.0.0.3", kommentar="frisch angelegt")
    z = db.liste()[0]
    assert z["zaehler"] == {30: 0, 90: 0, 180: 0, 360: 0}
    assert z["letzte_mail"] == ""


def test_speichern_ueberschreibt_nicht_uebergebene_felder_nicht(db):
    db.speichern("10.0.0.4", kommentar="Kopierer", ansprechpartner="Herr Ott",
                 extern=True)
    db.speichern("10.0.0.4", kommentar="Kopierer EG")
    e = db.host("10.0.0.4")
    assert e["kommentar"] == "Kopierer EG"
    assert e["ansprechpartner"] == "Herr Ott", "nicht übergeben = unverändert"
    assert e["extern"] == 1


def test_entfernen_raeumt_auch_die_zaehler_weg(db):
    db.speichern("10.0.0.5")
    db.merke_zustellung("10.0.0.5")
    assert db.entfernen("10.0.0.5")
    assert db.host("10.0.0.5") is None
    with relay_hosts._conn() as c:
        rest = c.execute("SELECT COUNT(*) c FROM tage WHERE ip='10.0.0.5'").fetchone()["c"]
    assert rest == 0, (
        "Bleiben die Zähler stehen, erbt ein später gleich adressiertes Gerät "
        "die Zahlen seines Vorgängers.")


def test_aufraeumen_trifft_nur_alte_zaehler(db):
    db.speichern("10.0.0.6")
    _eintragen("10.0.0.6", 10, 3)
    _eintragen("10.0.0.6", 500, 7)
    assert db.aufraeumen() == 1, "nur die alte Zeile"
    assert db.host("10.0.0.6") is not None, "das Gerät selbst bleibt"
    assert db.liste()[0]["zaehler"][30] == 3


def test_lerne_legt_nur_einmal_an(db):
    assert db.lerne("10.0.0.7") is True
    assert db.lerne("10.0.0.7") is False, "beim zweiten Mal ist es nicht mehr neu"
    assert db.host("10.0.0.7")["gelernt"] == 1


def test_lerne_hebt_eine_sperre_nicht_auf(db):
    """⚠️ Das gleiche Risiko wie im Prüfpfad, hier auf Datenebene.

    `INSERT OR IGNORE` darf einen bestehenden Eintrag nicht anfassen — sonst
    setzte ein Lernlauf `gesperrt` zurück, obwohl niemand das entschieden hat.
    """
    db.speichern("10.0.0.8", gesperrt=True, kommentar="stillgelegt")
    db.lerne("10.0.0.8")
    e = db.host("10.0.0.8")
    assert e["gesperrt"] == 1
    assert e["kommentar"] == "stillgelegt"


def test_liste_sortiert_nach_letzter_mail(db):
    db.speichern("10.0.0.9")
    db.speichern("10.0.0.10")
    db.merke_zustellung("10.0.0.10")
    assert [z["ip"] for z in db.liste()][0] == "10.0.0.10"


def test_abweisungsliste_haelt_den_letzten_stand(db):
    db.merke_abweisung("10.0.0.11", "a@x.de", ["b@y.de"], "Grund eins")
    db.merke_abweisung("10.0.0.11", "c@x.de", ["d@y.de"], "Grund zwei")
    e = db.abgewiesene()[0]
    assert e["anzahl"] == 2
    assert e["absender"] == "c@x.de", "der letzte Versuch ist der interessante"
    assert e["grund"] == "Grund zwei"


def test_abweisungen_leeren(db):
    db.merke_abweisung("10.0.0.12", "a@x.de", ["b@y.de"], "x")
    db.merke_abweisung("10.0.0.13", "a@x.de", ["b@y.de"], "x")
    assert db.abweisungen_leeren() == 2
    assert db.abgewiesene() == []


def test_kommentieren_hebt_eine_sperre_nicht_auf(db):
    """⚠️ Die Gegenrichtung zu `test_speichern_ueberschreibt_…` — und die mit
    Aussenwirkung.

    Der Fall ist naheliegend: Ein Gerät wird gesperrt, jemand trägt später den
    Grund als Kommentar nach. Würde `gesperrt` dabei auf 0 zurückfallen, wäre
    das Gerät wieder frei — ausgelöst durch eine Handlung, die mit der Sperre
    nichts zu tun hat, und ohne dass irgendwo etwas davon stünde.

    Eine Mutation, die `None` zu `0` macht, blieb ohne diesen Test unbemerkt.
    """
    db.speichern("10.0.0.20", gesperrt=True)
    db.speichern("10.0.0.20", kommentar="stillgelegt, Gerät ausgemustert")
    assert db.host("10.0.0.20")["gesperrt"] == 1

    # Und dieselbe Falle bei `extern`: Wer nur den Ansprechpartner nachträgt,
    # darf einem Gerät nicht nebenbei den Versand nach aussen wegnehmen.
    db.speichern("10.0.0.21", extern=True)
    db.speichern("10.0.0.21", ansprechpartner="Frau Meier")
    assert db.host("10.0.0.21")["extern"] == 1
