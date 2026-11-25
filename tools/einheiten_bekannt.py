#!/usr/bin/env python3
"""einheiten_bekannt — SHA-256 aller je ausgelieferten Fassungen der systemd-Units.

Der Updater (app/update_tausch.py) zieht die Units unter /etc/systemd/system mit, wenn sie
einer BEKANNTEN Fassung entsprechen. Weicht eine installierte Unit von jeder bekannten ab, hat
der Betreiber sie angepasst — dann tauscht er nicht, sondern meldet es (Drop-ins unter
*.service.d bleiben ohnehin unberührt).

    python3 tools/einheiten_bekannt.py schreiben   # aus der Git-Historie von linux/ (alle Commits)
    python3 tools/einheiten_bekannt.py pruefen     # aktuelle Units stehen in der Liste (auch Test)

Nach JEDER Änderung an einer Unit: `schreiben` (tests/test_aktualisierung.py prüft es).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
LINUX = WURZEL / "linux"
DATEI = LINUX / "einheiten-bekannt.json"
EINHEITEN = ("exo-smtp-relay.service", "exo-smtp-relay-update.service", "exo-smtp-relay-update.path")


def _git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(WURZEL), *args], capture_output=True, check=True).stdout


def aus_historie() -> dict[str, list[str]]:
    aus: dict[str, set[str]] = {n: set() for n in EINHEITEN}
    for name in EINHEITEN:
        for c in _git("log", "--all", "--format=%H", "--", f"linux/{name}").decode().split():
            try:
                aus[name].add(hashlib.sha256(_git("show", f"{c}:linux/{name}")).hexdigest())
            except subprocess.CalledProcessError:
                pass                                   # in diesem Commit gelöscht
        aus[name].add(hashlib.sha256((LINUX / name).read_bytes()).hexdigest())
    return {n: sorted(h) for n, h in aus.items()}


def fehlende() -> list[str]:
    bekannt = json.loads(DATEI.read_text(encoding="utf-8"))
    return [n for n in EINHEITEN
            if hashlib.sha256((LINUX / n).read_bytes()).hexdigest() not in bekannt.get(n, [])]


if __name__ == "__main__":
    if sys.argv[1:] == ["schreiben"]:
        DATEI.write_text(json.dumps(aus_historie(), indent=1) + "\n", encoding="utf-8")
        print(f"{DATEI.relative_to(WURZEL)} geschrieben")
        sys.exit(0)
    if sys.argv[1:] == ["pruefen"]:
        f = fehlende()
        print("OK" if not f else "FEHLT: " + ", ".join(f))
        sys.exit(1 if f else 0)
    print(__doc__)
    sys.exit(2)
