#!/usr/bin/env python3
"""driftcheck — gespiegelte Dateien müssen mit dem grossen Gateway inhaltsgleich sein.

Das Relay teilt sich mit dem EXO Signature Gateway die Regeln (`smtp_relay.py`),
die Geräteliste (`relay_hosts.py`) und einige Bausteine. Sie sind KOPIEN, kein
Paket — bewusst, damit jeder Dienst für sich installierbar bleibt. Der Preis
ist, dass zwei Kopien auseinanderlaufen können. Dieses Skript ist der Preis.

AUFRUF
    python3 tools/driftcheck.py                  # sucht das Gateway daneben
    python3 tools/driftcheck.py --gateway PFAD   # ausdrücklich

Rückgabe 1 bei einer Abweichung, 0 wenn alles gleich ist — oder 0 mit Hinweis,
wenn kein Gateway-Baum gefunden wurde (in der CI des eigenen Repos).
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

RELAY = Path(__file__).resolve().parent.parent

# Dateien, die in beiden Anwendungen inhaltsgleich sein MÜSSEN.
MIRRORED: list[tuple[str, str]] = [
    ("app/smtp_relay.py", "die drei Grenzen des Relays und der Lernmodus"),
    ("app/relay_hosts.py", "Geräteliste, Zähler, Abweisungen"),
    ("app/secure_io.py", "Schreiben von Geheimnissen (600/700, atomar)"),
    ("app/smtp_rauschen.py", "abgebrochene Fremdverbindungen leiser"),
    ("app/mail_trace.py", "Trace-ID je Nachricht"),
    ("app/runtime_state.py", "prozessweiter Laufzeitzustand"),
    ("app/login_drossel.py", "Backoff nach Fehlversuchen"),
    ("app/webui/static/common.js", "gemeinsame Frontend-Helfer"),
    ("app/webui/static/style.css", "Stylesheet"),
    ("app/webui/static/dark-mode.css", "Dunkelmodus"),
]

# Das Gateway daneben (eigenes Repo: ../EXO-Signature-Gateway) oder darüber
# (Monorepo-Phase, als der Baum unter relay/ lag).
KANDIDATEN = (RELAY.parent / "EXO-Signature-Gateway", RELAY.parent, RELAY.parent.parent / "EXO-Signature-Gateway")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def gateway_finden(pfad: str | None) -> Path | None:
    if pfad:
        return Path(pfad)
    for k in KANDIDATEN:
        if (k / "app" / "smtp_relay.py").is_file() and (k / "app" / "handler.py").is_file() \
                and (k / "Dockerfile").is_file():
            return k
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", help="Pfad zum EXO-Signature-Gateway-Baum")
    args = ap.parse_args()
    gw = gateway_finden(args.gateway)
    if gw is None:
        print("driftcheck: kein Gateway-Baum gefunden — Spiegelprüfung übersprungen "
              "(läuft beim Gateway-Lauf, wo beide Bäume vorliegen).")
        return 0
    probleme = 0
    for rel, zweck in MIRRORED:
        a, b = RELAY / rel, gw / rel
        if not a.is_file() or not b.is_file():
            print(f"FEHLT  {rel} — {'Relay' if not a.is_file() else 'Gateway'} ({zweck})")
            probleme += 1
            continue
        if _sha(a) != _sha(b):
            print(f"DRIFT  {rel} — {zweck}")
            probleme += 1
    if probleme:
        print(f"\n{probleme} Abweichung(en) gegenüber {gw}")
        return 1
    print(f"driftcheck: {len(MIRRORED)} gespiegelte Dateien inhaltsgleich mit {gw}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
