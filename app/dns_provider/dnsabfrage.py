# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Alexander Zarenko / Alexander Zarenko IT Consulting (AZITC)
# Herkunft: EXO SMTP Relay (github.com/azitc-ac/exo-smtp-relay), app/dns_provider/.
# Teil des gemeinsamen DNS-Provider-Katalogs — siehe __init__.py und KATALOG_VERSION.
"""Schlanke DNS-Abfrage gegen die AUTORITATIVEN Nameserver — nur Standardbibliothek.

Warum selbst und nicht dnspython: Das Relay hat dnspython nicht, und jede neue
Abhängigkeit muss mit Prüfsummen in die Lock-Datei und ins Offline-Paket. Für
die Frage „steht unser TXT-Wert schon auf JEDEM autoritativen Nameserver?“
reichen ~150 Zeilen: TXT-Abfrage ohne Rekursion (RD=0), Verweisen von den
Wurzelservern abwärts folgen, bei abgeschnittener Antwort (TC) über TCP.

Warum die autoritativen und nicht der eigene Resolver: Ein Resolver merkt sich
auch die NEGATIVE Antwort („gibt es nicht“) für die Dauer der SOA-Minimum-TTL —
wer zu früh fragt, sieht den neuen Record dort minutenlang nicht, obwohl Let's
Encrypt (das selbst autoritativ fragt) ihn längst sähe. Umgekehrt sagt ein
Treffer bei EINEM Nameserver nichts: Let's Encrypt fragt irgendeinen.

Nameserver-Namen ohne Glue-Record werden über den Resolver des Systems
aufgelöst (`socket.getaddrinfo`) — das ist eine A-Abfrage, kein TXT, und
unproblematisch. Nur IPv4, wenn vorhanden (IPv6 ist oft nicht geroutet).

GRENZEN: Ein CNAME auf `_acme-challenge` (Delegation an einen anderen Dienst)
wird nicht verfolgt — der Wert gilt dann nie als sichtbar. Ist Port 53 nach
außen gesperrt, wirft `warten()` nach dem Zeitlimit `DnsNichtErreichbar`; der
Aufrufer entscheidet, ob er trotzdem weitermacht.
"""
from __future__ import annotations

import secrets
import socket
import struct
import time

PORT = 53
ZEITLIMIT_ABFRAGE = 3.0
# IPv4 der 13 Wurzelserver (root-servers.org, Stand 2026; b.root seit 11/2023 neu).
WURZELN = ("198.41.0.4", "170.247.170.2", "192.33.4.12", "199.7.91.13", "192.203.230.10",
           "192.5.5.241", "192.112.36.4", "198.97.190.53", "192.36.148.17", "192.58.128.30",
           "193.0.14.129", "199.7.83.42", "202.12.27.33")
_TYP_A, _TYP_NS, _TYP_CNAME, _TYP_TXT, _TYP_AAAA = 1, 2, 5, 16, 28
_MAX_VERWEISE = 12


class DnsAbfrageFehler(Exception):
    pass


class DnsNichtErreichbar(DnsAbfrageFehler):
    """Kein Nameserver hat geantwortet (Port 53 gesperrt?)."""


class PropagationZeitueberschreitung(DnsAbfrageFehler):
    """Der Wert war bis zum Zeitlimit nicht auf allen autoritativen Servern."""


# ── Drahtformat ──────────────────────────────────────────────────────────────

def _name_kodieren(name: str) -> bytes:
    aus = b""
    for teil in name.strip(".").split("."):
        b = teil.encode("idna") if teil else b""
        if not 0 < len(b) < 64:
            raise DnsAbfrageFehler(f"ungültiger Name: {name}")
        aus += bytes([len(b)]) + b
    return aus + b"\x00"


def frage_bauen(name: str, typ: int, kennung: int) -> bytes:
    # Flags 0: Standardabfrage, RD=0 (keine Rekursion — wir fragen autoritativ)
    return struct.pack(">HHHHHH", kennung, 0, 1, 0, 0, 0) + _name_kodieren(name) + struct.pack(">HH", typ, 1)


def _name_lesen(daten: bytes, pos: int) -> tuple[str, int]:
    teile, ende, spruenge = [], None, 0
    while True:
        if pos >= len(daten):
            raise DnsAbfrageFehler("Antwort abgeschnitten")
        laenge = daten[pos]
        if laenge & 0xC0 == 0xC0:
            if pos + 1 >= len(daten):
                raise DnsAbfrageFehler("Antwort abgeschnitten")
            if ende is None:
                ende = pos + 2
            pos = ((laenge & 0x3F) << 8) | daten[pos + 1]
            spruenge += 1
            if spruenge > 64:
                raise DnsAbfrageFehler("Zeigerschleife in der Antwort")
            continue
        if laenge == 0:
            pos += 1
            break
        teile.append(daten[pos + 1:pos + 1 + laenge].decode("ascii", "replace"))
        pos += 1 + laenge
    return ".".join(teile).lower(), (ende if ende is not None else pos)


def antwort_lesen(daten: bytes) -> dict:
    """Antwort zerlegen: {kennung, aa, tc, rcode, antworten, autoritaet, zusatz}.
    Jeder Eintrag (name, typ, wert) — TXT als zusammengesetzter Text, NS/CNAME
    als Name, A/AAAA als Adresse."""
    if len(daten) < 12:
        raise DnsAbfrageFehler("Antwort zu kurz")
    kennung, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", daten[:12])
    pos = 12
    for _ in range(qd):
        _, pos = _name_lesen(daten, pos)
        pos += 4
    abschnitte = []
    for anzahl in (an, ns, ar):
        eintraege = []
        for _ in range(anzahl):
            name, pos = _name_lesen(daten, pos)
            if pos + 10 > len(daten):
                raise DnsAbfrageFehler("Antwort abgeschnitten")
            typ, _klasse, _ttl, laenge = struct.unpack(">HHIH", daten[pos:pos + 10])
            pos += 10
            roh = daten[pos:pos + laenge]
            if typ == _TYP_TXT:
                stuecke, i = [], 0
                while i < len(roh):
                    n = roh[i]
                    stuecke.append(roh[i + 1:i + 1 + n].decode("utf-8", "replace"))
                    i += 1 + n
                wert = "".join(stuecke)
            elif typ in (_TYP_NS, _TYP_CNAME):
                wert, _ = _name_lesen(daten, pos)
            elif typ == _TYP_A and laenge == 4:
                wert = socket.inet_ntop(socket.AF_INET, roh)
            elif typ == _TYP_AAAA and laenge == 16:
                wert = socket.inet_ntop(socket.AF_INET6, roh)
            else:
                wert = roh
            eintraege.append((name, typ, wert))
            pos += laenge
        abschnitte.append(eintraege)
    return {"kennung": kennung, "aa": bool(flags & 0x0400), "tc": bool(flags & 0x0200),
            "rcode": flags & 0x000F, "antworten": abschnitte[0], "autoritaet": abschnitte[1],
            "zusatz": abschnitte[2]}


def _senden(ip: str, frage: bytes) -> bytes:
    familie = socket.AF_INET6 if ":" in ip else socket.AF_INET
    with socket.socket(familie, socket.SOCK_DGRAM) as s:
        s.settimeout(ZEITLIMIT_ABFRAGE)
        s.sendto(frage, (ip, PORT))
        ende = time.monotonic() + ZEITLIMIT_ABFRAGE
        while True:
            daten, _ = s.recvfrom(65535)
            if daten[:2] == frage[:2]:           # fremde/verspätete Antworten verwerfen
                break
            if time.monotonic() > ende:
                raise socket.timeout()
    if len(daten) >= 4 and daten[2] & 0x02:      # TC: über TCP noch einmal
        with socket.create_connection((ip, PORT), timeout=ZEITLIMIT_ABFRAGE) as s:
            s.sendall(struct.pack(">H", len(frage)) + frage)
            laenge = struct.unpack(">H", _genau(s, 2))[0]
            daten = _genau(s, laenge)
    return daten


def _genau(s: socket.socket, n: int) -> bytes:
    puffer = b""
    while len(puffer) < n:
        stueck = s.recv(n - len(puffer))
        if not stueck:
            raise DnsAbfrageFehler("TCP-Verbindung vorzeitig geschlossen")
        puffer += stueck
    return puffer


def fragen(ip: str, name: str, typ: int = _TYP_TXT) -> dict:
    """Eine Abfrage an EINEN Server. Wirft DnsNichtErreichbar bei Zeitüberschreitung."""
    frage = frage_bauen(name, typ, secrets.randbits(16))
    try:
        return antwort_lesen(_senden(ip, frage))
    except (OSError, socket.timeout) as exc:
        raise DnsNichtErreichbar(f"{ip}: {type(exc).__name__}") from None


# ── Autoritative Server finden ───────────────────────────────────────────────

def _adressen(nsname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(nsname, PORT, proto=socket.IPPROTO_UDP)
    except OSError:
        return []
    v4 = [i[4][0] for i in infos if i[0] == socket.AF_INET]
    v6 = [i[4][0] for i in infos if i[0] == socket.AF_INET6]
    return list(dict.fromkeys(v4 or v6))


def autoritative_server(name: str) -> list[tuple[str, str]]:
    """[(Nameserver-Name, IP)] — je autoritativem Nameserver eine Adresse.
    Folgt den Verweisen ab den Wurzelservern."""
    server: list[tuple[str, str]] = [(f"root-{i}", ip) for i, ip in enumerate(WURZELN)]
    for _ in range(_MAX_VERWEISE):
        antwort, letzter = None, None
        for _ns, ip in server:
            try:
                antwort = fragen(ip, name)
                break
            except DnsNichtErreichbar as exc:
                letzter = exc
        if antwort is None:
            raise DnsNichtErreichbar(f"kein Nameserver erreichbar ({letzter})")
        if antwort["aa"]:
            return server
        ns_namen = [w for (_n, t, w) in antwort["autoritaet"] if t == _TYP_NS]
        if not ns_namen:
            raise DnsAbfrageFehler(f"keine Delegation für {name} (rcode {antwort['rcode']})")
        glue: dict[str, list[str]] = {}
        for (n, t, w) in antwort["zusatz"]:
            if t == _TYP_A:
                glue.setdefault(n, []).append(w)
        for (n, t, w) in antwort["zusatz"]:
            if t == _TYP_AAAA and n not in glue:
                glue.setdefault(n, []).append(w)
        neu = []
        for ns in dict.fromkeys(ns_namen):
            ips = glue.get(ns) or _adressen(ns)
            if ips:
                neu.append((ns, ips[0]))
        if not neu:
            raise DnsNichtErreichbar(f"Adressen der Nameserver {', '.join(ns_namen)} nicht auflösbar")
        server = neu
    raise DnsAbfrageFehler(f"zu viele Verweise für {name}")


def txt_werte(ip: str, name: str) -> list[str]:
    a = fragen(ip, name, _TYP_TXT)
    return [w for (n, t, w) in a["antworten"] if t == _TYP_TXT and n == name.strip(".").lower()]


def warten(name: str, wert: str, zeitlimit: float, intervall: float = 10.0,
           schlafen=time.sleep) -> list[str]:
    """Warten, bis `wert` unter `name` auf JEDEM autoritativen Nameserver steht.

    Rückgabe: die geprüften Nameserver. Wirft PropagationZeitueberschreitung
    (Server erreicht, Wert fehlt) oder DnsNichtErreichbar (nie ein Server
    erreicht) nach `zeitlimit` Sekunden."""
    name = name.strip(".").lower()
    ende = time.monotonic() + zeitlimit
    server: list[tuple[str, str]] | None = None
    erreicht = False
    fehlend: list[str] = []
    letzter = ""
    while True:
        try:
            if server is None:
                server = autoritative_server(name)
            fehlend = []
            for ns, ip in server:
                try:
                    if wert not in txt_werte(ip, name):
                        fehlend.append(ns)
                    erreicht = True
                except DnsAbfrageFehler as exc:
                    fehlend.append(f"{ns} ({exc})")
            if not fehlend:
                return [ns for ns, _ in server]
        except DnsNichtErreichbar as exc:
            letzter = str(exc)
        except DnsAbfrageFehler as exc:
            letzter = str(exc)
            erreicht = True
        if time.monotonic() >= ende:
            if not erreicht:
                raise DnsNichtErreichbar(f"DNS-Prüfung für {name} nicht möglich: {letzter} — "
                                         f"ist Port 53 (UDP/TCP) nach außen gesperrt?")
            raise PropagationZeitueberschreitung(
                f"TXT {name} nach {int(zeitlimit)} s nicht auf allen autoritativen Nameservern: "
                f"es fehlt bei {', '.join(fehlend) or letzter}")
        schlafen(intervall)
