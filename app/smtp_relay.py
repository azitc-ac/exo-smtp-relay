"""SMTP-Relay für Geräte im eigenen Netz — Ersatz für einen Exchange vor Ort.

ANLASS (2026-08-25)
-------------------
Wiederkehrende Anforderung auf Kundenseite: Drucker, Scanner und Anwendungen
liefern seit Jahren anonym per SMTP bei einem lokalen Exchange ab. Wird der
abgelöst, müssten alle Geräte umgestellt werden — was niemand will. Ein Gateway
vor Ort steht ohnehin und ist mit Exchange Online verbunden.

⚠️ DAS GATEWAY RELAYT HEUTE SCHON — unbeabsichtigt
--------------------------------------------------
`handler.py` reicht jede Nachricht unverändert weiter, deren Absender nicht in
`MAILBOX_CONFIG` steht. Der einzige Schutz ist die Quell-IP-Prüfung. Wer dort
ein Netz einträgt, hat ab diesem Moment ein Relay — ohne Absenderprüfung, ohne
Zielbeschränkung, ohne dass irgendwo stünde, dass das geschieht.

Dieses Modul macht daraus eine bewusste Entscheidung mit Grenzen. Es ist
deshalb KEIN Zugewinn an Fähigkeit, sondern einer an Kontrolle.

DIE DREI GRENZEN
----------------
1. **Gerät** — nur Adressen aus der Geräteliste (`relay_hosts`).
   ⚠️ Daneben gab es bis v1.8.14 eine zweite Liste (`SMTP_ACL_EXTRA_CIDRS`,
   „zusätzlich erlaubte Netze" unter Erweitert). Sie ist entfallen: Zwei Wege,
   ein Netz freizugeben, von denen nur einer Absender und Ziel prüft und nur
   einer protokolliert, sind einer zu viel.
2. **Absender** — nur Domänen, die dem Tenant gehören. Ein übernommener
   Drucker soll nicht als fremde Firma versenden können.
3. **Ziel** — Vorgabe: nur Empfänger im eigenen Tenant, je Gerät umstellbar.
   Nach aussen muss auch der Exchange-Connector das Weiterleiten erlauben
   (sonst `550 5.7.54`).

DER LERNMODUS
-------------
Eine Geräteliste von Hand zu füllen setzt voraus, dass man alle Geräte kennt —
und genau das weiss beim Ablösen eines gewachsenen Exchange niemand. Der
Lernmodus schaltet deshalb einen Bereich **befristet** frei
(`SMTP_RELAY_LERN_BIS`, höchstens zwei Stunden) und legt für jedes Gerät, das
darin etwas Zulässiges einliefert, einen Eintrag an. Der Bereich darf als Netz
(`192.168.1.0/24`) oder als Spanne (`172.16.16.10-172.16.17.20`) stehen.
Danach läuft es weiter; ergänzt werden nur noch Kommentar und Ansprechpartner.

⚠️ Ein Lernbereich ist **kein** Freibrief. Ausserhalb des Zeitfensters lässt er
nichts durch — sonst wäre der Unterschied zur alten Netzliste nur ein Name.

⚠️ NUR IM MODUS `smtp`
----------------------
Der Rückweg entscheidet, ob ein Relay überhaupt funktionieren kann. Nur der
Smarthost-Weg reicht eine Nachricht unverändert weiter — mit dem Absender, den
das Gerät gesetzt hat. Die anderen Wege können das nicht:

  `graph`  Graph sendet immer „als" ein Postfach. Ein Drucker hat keines;
           Graph antwortet `ErrorInvalidUser`.
  `imap`   APPEND legt die Nachricht in ein Zielpostfach — für interne Ziele
           denkbar, für externe nicht, und der Weg ist dafür nie erprobt.

Diese Grenze steht deshalb HIER und nicht nur in der Oberfläche: Wer den Modus
später umstellt, bekäme sonst ein Relay, das Post annimmt und dann verwirft.

⚠️ STARTTLS IST FÜR DIESE GERÄTE KEINE PFLICHT
----------------------------------------------
Ein Etikettendrucker von 2011 kann kein STARTTLS. `main._LenientSMTP.
connection_made()` nimmt die Pflicht deshalb für jede Adresse zurück, die
`ist_relay_quelle()` bejaht — und nur für die. Angeboten wird STARTTLS
weiterhin; ob ein Gerät es nutzt, hält `relay_hosts` je Gerät fest, sonst wäre
nach der Lockerung nicht mehr erkennbar, wer im Klartext liefert.

⚠️ ZUR AUSFALLRICHTUNG
----------------------
`smtp_acl.is_allowed()` lässt bei leerer Adressliste ALLES durch — bewusst, um
den Mailfluss nicht zu unterbrechen. Für ein Relay ist diese Richtung falsch:
Kennt das Gateway seine eigenen Adressen nicht, kann es weder Absender noch
Empfänger beurteilen. Dann wird das Relay verweigert. Der reguläre Mailfluss
bleibt davon unberührt — er läuft über einen anderen Zweig.
"""
from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Ergebnis einer Prüfung: (erlaubt, Grund fürs Protokoll, SMTP-Antwort)
ERLAUBT = (True, "", "")

# Wie lange darf ein Lernlauf höchstens dauern, und wie lange schlägt die
# Oberfläche vor. Der Lernmodus ist die einzige Betriebsart, in der sich die
# Freigabe von selbst erweitert — er gehört kurz gehalten. Eine Viertelstunde
# reicht, um an ein paar Geräten einen Testdruck auszulösen.
MAX_LERNDAUER_MIN = 120
STANDARD_LERNDAUER_MIN = 15


def _lernbereiche() -> list[tuple[int, int, int]]:
    """Die Lernbereiche als (Version, erste, letzte) Adresse — als Zahlen.

    Zwei Schreibweisen sind zugelassen, weil beide im Alltag vorkommen:

        192.168.1.0/24                    ein Netz
        172.16.16.10-172.16.17.20         ein Bereich über Netzgrenzen hinweg

    Der Bereich lässt sich nicht als Netz ausdrücken (er beginnt und endet
    mitten drin), und ihn in mehrere `/nn` zu zerlegen wäre eine Rechnung, die
    niemand nachvollziehen will. Ein Zahlenpaar beantwortet beide Fälle.

    Die Version wird mitgeführt, damit eine IPv6-Adresse nicht rechnerisch in
    einen IPv4-Bereich fallen kann.
    """
    import settings_store
    aus: list[tuple[int, int, int]] = []
    for eintrag in settings_store.get("SMTP_RELAY_LERN_NETZE") or []:
        text = str(eintrag).strip()
        if not text:
            continue
        try:
            if "-" in text:
                von, _, bis = text.partition("-")
                a = ipaddress.ip_address(von.strip())
                b = ipaddress.ip_address(bis.strip())
                if a.version != b.version:
                    raise ValueError("gemischte Adressfamilien")
                erste, letzte = sorted((int(a), int(b)))
                aus.append((a.version, erste, letzte))
            else:
                n = ipaddress.ip_network(text, strict=False)
                aus.append((n.version, int(n.network_address),
                            int(n.broadcast_address)))
        except ValueError:
            log.warning("SMTP-Relay: %r ist kein gültiges Netz und kein "
                        "gültiger Bereich — übergangen", text)
    return aus


def lernmodus_bis() -> datetime | None:
    """Bis wann läuft der Lernmodus? None, wenn er aus oder abgelaufen ist.

    ⚠️ Das Ablaufen geschieht durch LESEN, nicht durch Schreiben. Ein Auftrag,
    der die Einstellung zum Stichzeitpunkt zurücksetzt, wäre eine zweite Stelle,
    an der die Wahrheit stünde — und liefe bei einem Neustart im falschen
    Moment gar nicht. Der Zeitpunkt allein entscheidet.

    ⚠️ Die Höchstdauer wird HIER gedeckelt, nicht nur im Formular. Wer einen
    Zeitpunkt in ferner Zukunft in die Konfigurationsdatei schreibt — von Hand
    oder aus einer Sicherung — hätte sonst ein dauerhaft lernendes Gateway,
    also genau das offene Relay, das zu vermeiden der Zweck der ganzen
    Konstruktion ist.
    """
    import settings_store
    roh = (settings_store.get("SMTP_RELAY_LERN_BIS") or "").strip()
    if not roh:
        return None
    try:
        bis = datetime.fromisoformat(roh.replace("Z", "+00:00"))
    except ValueError:
        log.warning("SMTP-Relay: %r ist kein Zeitpunkt — Lernmodus gilt als aus", roh)
        return None
    if bis.tzinfo is None:
        bis = bis.replace(tzinfo=timezone.utc)
    jetzt = datetime.now(timezone.utc)
    if bis <= jetzt:
        return None
    grenze = jetzt + timedelta(minutes=MAX_LERNDAUER_MIN)
    if bis > grenze:
        log.warning("SMTP-Relay: Lernmodus war bis %s eingetragen — auf die "
                    "Höchstdauer von %d Minuten gekürzt",
                    bis.strftime("%Y-%m-%d %H:%M"), MAX_LERNDAUER_MIN)
        return grenze
    return bis


def _im_lernbereich(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    wert = int(addr)
    return any(v == addr.version and erste <= wert <= letzte
               for v, erste, letzte in _lernbereiche())


def ist_relay_quelle(ip: str) -> bool:
    """Darf von dieser Adresse eingeliefert werden?

    Zwei Wege führen hierher: Das Gerät steht in der Geräteliste — oder der
    Lernmodus läuft und die Adresse liegt in einem Lernnetz. Ein Lernnetz
    allein lässt NICHTS durch; es ist kein Freibrief, sondern der Bereich, in
    dem gelernt werden darf.
    """
    import settings_store
    # `get_bool()` gibt es nur im Hub (settings_schema); im Gateway ist die
    # schlichte Wahrheitsprüfung das übliche Muster für einen Schalter mit
    # Vorgabe AUS — siehe reinject.py bei GRAPH_SMTP_FALLBACK.
    if not settings_store.get("SMTP_RELAY_ENABLED"):
        return False
    ip = (ip or "").strip()
    if not ip:
        return False

    import relay_hosts
    eintrag = relay_hosts.host(ip)
    if eintrag is not None:
        # ⚠️ Eine Sperre schlägt den Lernmodus — und zwar durch das frühe
        # `return`. Wer hier nur `if eintrag and not gesperrt: return True`
        # schriebe und dann weiterliefe, hätte ein gesperrtes Gerät, das beim
        # nächsten Lernlauf im selben Netz wieder senden darf. Die Sperre wäre
        # dann eine Empfehlung, kein Verbot.
        if eintrag.get("gesperrt"):
            log.info("SMTP-Relay: %s ist gesperrt — abgewiesen", ip)
            return False
        return True
    return bool(lernmodus_bis()) and _im_lernbereich(ip)


def _eigene_domaenen() -> set[str]:
    """Domänen, die dem Tenant gehören — aus den bekannten Postfachadressen.

    Zusätzlich `TENANT_DOMAIN`, weil die Adressliste Aliasdomänen führt, die
    Startdomäne `…onmicrosoft.com` aber nicht zwingend als Postfachadresse
    auftaucht.
    """
    import exo_mailboxes
    import settings_store
    domaenen = {a.rsplit("@", 1)[-1].lower()
                for a in exo_mailboxes.known_addresses() if "@" in a}
    tenant = (settings_store.get("TENANT_DOMAIN") or "").strip().lower()
    if tenant:
        domaenen.add(tenant)
    return {d for d in domaenen if d}


def pruefe(absender: str, empfaenger: list[str], ip: str) -> tuple[bool, str, str]:
    """Darf diese Nachricht über das Relay? → (erlaubt, Protokollgrund, SMTP-Antwort).

    Wird NUR aufgerufen, wenn `ist_relay_quelle()` bereits zugestimmt hat.
    """
    import exo_mailboxes
    import settings_store

    # ⚠️ Die Ausfallrichtung hängt an den ADRESSEN, nicht an den Domänen.
    #
    # Der erste Entwurf prüfte `_eigene_domaenen()` auf leer — die Menge ist
    # aber nie leer, sobald `TENANT_DOMAIN` gesetzt ist (und das ist sie nach
    # jeder Einrichtung). Die Sicherung wäre damit tot gewesen, und der Test
    # dazu hätte grün gemeldet, was nie greift. Massgeblich ist die
    # Postfachliste: Ohne sie lässt sich kein Ziel beurteilen.
    # ⚠️ Siehe Modulkopf: nur der Smarthost-Weg reicht fremde Absender
    # unveraendert weiter. Die Oberflaeche bietet das Relay deshalb nur im
    # Modus `smtp` an — durchgesetzt wird es hier, weil der Modus danach noch
    # umgestellt werden kann.
    modus = (settings_store.get("REINJECT_MODE") or "smtp").strip()
    if modus != "smtp":
        return (False,
                f"Relay von {ip} abgelehnt — der Rückweg steht auf {modus!r}; "
                "das Relay setzt den SMTP-Smarthost voraus",
                "451 4.3.2 Relay in dieser Betriebsart nicht möglich")

    adressen = exo_mailboxes.known_addresses()
    if not adressen:
        return (False,
                f"Relay von {ip} abgelehnt — die Postfachliste ist (noch) nicht "
                "bekannt, Absender und Ziel lassen sich nicht prüfen",
                "451 4.3.2 Relay temporär nicht verfügbar")

    bekannt = _eigene_domaenen()

    absender = (absender or "").strip().lower()
    domain = absender.rsplit("@", 1)[-1] if "@" in absender else ""
    if domain not in bekannt:
        return (False,
                f"Relay von {ip} abgelehnt — Absenderdomäne {domain or '(leer)'} "
                "gehört nicht zu diesem Tenant",
                "550 5.7.1 Absenderdomäne für das Relay nicht zulässig")

    # Erst hier wird gelernt, nicht in `ist_relay_quelle()`: Ein Gerät soll in
    # die Liste kommen, wenn es etwas Zulässiges einliefert — nicht schon, weil
    # es eine Verbindung aufgebaut hat. Ein Portscan im Lernnetz füllte sonst
    # die Tabelle mit Adressen, die nie eine Mail geschickt haben.
    import relay_hosts
    eintrag = relay_hosts.host(ip)
    if eintrag is None:
        extern_vorgabe = bool(settings_store.get("SMTP_RELAY_EXTERN_VORGABE"))
        relay_hosts.lerne(ip, extern=extern_vorgabe)
        eintrag = {"extern": 1 if extern_vorgabe else 0}

    if eintrag.get("extern"):
        return ERLAUBT

    # Nur interne Ziele: gegen die bekannten ADRESSEN prüfen, nicht gegen die
    # Domänen. Eine Adresse der eigenen Domäne, die es nicht gibt, ist kein
    # internes Ziel — Exchange erzeugte daraus einen Unzustellbarkeitsbericht
    # nach aussen, also doch eine Zustellung nach draussen.
    fremd = [e for e in empfaenger if (e or "").strip().lower() not in adressen]
    if fremd:
        return (False,
                f"Relay von {ip} abgelehnt — Empfänger ausserhalb des Tenants: "
                + ", ".join(fremd[:3]),
                "550 5.7.1 Relay nur an Empfänger im eigenen Tenant")

    return ERLAUBT
