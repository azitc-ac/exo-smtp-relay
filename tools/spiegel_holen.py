#!/usr/bin/env python3
"""spiegel_holen — gespiegelte Dateien aus dem Gateway übernehmen.

`driftcheck.py` MELDET Abweichungen; dieses Skript BEHEBT sie in der einen
Richtung, in der sie im Alltag entstehen: Das Gateway ändert eine Regel in
`smtp_relay.py`, und das Relay soll nachziehen.

AUFRUF
    python3 tools/spiegel_holen.py                 # zeigt, was abweicht (ändert nichts)
    python3 tools/spiegel_holen.py --uebernehmen   # kopiert die abweichenden Dateien
    python3 tools/spiegel_holen.py --gateway PFAD  # Gateway-Baum ausdrücklich

Vor dem Kopieren zeigt es je Datei den letzten Gateway-Commit, der sie berührt
hat — man soll sehen, WAS herüberkommt, nicht nur DASS etwas herüberkommt.
Eine Regeländerung im Relay-Kern liest man vor dem Übernehmen.

⚠️ Nur diese Richtung. Wurde eine gespiegelte Datei im RELAY geändert, meldet
das Skript das („Relay ist neuer") und kopiert NICHT — sonst überschriebe der
nächtliche Lauf eine bewusste Änderung. Die Gegenrichtung ist Handarbeit im
Gateway-Repo.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import driftcheck  # noqa: E402

RELAY = driftcheck.RELAY


def _letzter_commit(repo: Path, rel: str) -> str:
    """„<datum> <kurz-sha> <betreff>" des letzten Commits, der die Datei berührt."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%cs %h %s", "--", rel],
            capture_output=True, text=True, timeout=20).stdout.strip()
        return out or "(kein Commit bekannt)"
    except Exception:                                         # noqa: BLE001
        return "(git nicht verfügbar)"


def _datum(repo: Path, rel: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%cI", "--", rel],
            capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:                                         # noqa: BLE001
        return ""


def abweichungen(gw: Path) -> list[dict]:
    """Je abweichender Datei: Pfad, Zweck, Herkunft, und ob das Relay neuer ist."""
    aus = []
    for rel, zweck in driftcheck.MIRRORED:
        a, b = RELAY / rel, gw / rel
        if not b.is_file():
            aus.append({"rel": rel, "zweck": zweck, "fehlt_im_gateway": True})
            continue
        if not a.is_file() or driftcheck._sha(a) != driftcheck._sha(b):
            relay_datum, gw_datum = _datum(RELAY, rel), _datum(gw, rel)
            aus.append({"rel": rel, "zweck": zweck, "fehlt_im_gateway": False,
                        "gateway_commit": _letzter_commit(gw, rel),
                        "relay_commit": _letzter_commit(RELAY, rel),
                        # Ohne Datum (kein git, frische Kopie) gilt das Gateway als Quelle.
                        "relay_neuer": bool(relay_datum and gw_datum and relay_datum > gw_datum)})
    return aus


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", help="Pfad zum EXO-Signature-Gateway-Baum")
    ap.add_argument("--uebernehmen", action="store_true", help="abweichende Dateien kopieren")
    args = ap.parse_args()
    gw = driftcheck.gateway_finden(args.gateway)
    if gw is None:
        print("spiegel_holen: kein Gateway-Baum gefunden (--gateway PFAD)")
        return 2
    liste = abweichungen(gw)
    if not liste:
        print(f"spiegel_holen: alle {len(driftcheck.MIRRORED)} gespiegelten Dateien sind gleich ({gw})")
        return 0
    rc = 0
    for e in liste:
        if e["fehlt_im_gateway"]:
            print(f"FEHLT   {e['rel']} — im Gateway nicht vorhanden ({e['zweck']})")
            rc = 1
            continue
        print(f"{'RELAY-NEUER' if e['relay_neuer'] else 'ABWEICHUNG '} {e['rel']} — {e['zweck']}")
        print(f"            Gateway: {e['gateway_commit']}")
        print(f"            Relay:   {e['relay_commit']}")
        if e["relay_neuer"]:
            print("            → nicht übernommen: die Änderung liegt im Relay; ins Gateway zurückspielen.")
            rc = 1
            continue
        if args.uebernehmen:
            (RELAY / e["rel"]).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(gw / e["rel"], RELAY / e["rel"])
            print("            → übernommen")
        else:
            rc = 1
    if not args.uebernehmen and rc:
        print("\nÜbernehmen mit: python3 tools/spiegel_holen.py --uebernehmen")
    return rc


if __name__ == "__main__":
    sys.exit(main())
