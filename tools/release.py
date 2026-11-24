#!/usr/bin/env python3
"""Release-Werkzeug — von .github/workflows/release.yml aufgerufen, lokal nutzbar.

    python tools/release.py kanal v0.2.40            Tag prüfen; version=/kanal= ausgeben
    python tools/release.py bauen dist               Windows-ZIP, Linux-Archiv, SHA256SUMS
    python tools/release.py notizen 0.2.40           Abschnitt aus CHANGELOG.md
    python tools/release.py signieren dist/SHA256SUMS
                                 Schlüssel aus RELEASE_SIGNING_KEY (PEM); fehlt er → Abbruch
    python tools/release.py pruefen dist             Signatur + Prüfsummen gegen den EINGEBAUTEN
                                                     Schlüssel (app/update_paket.py)
    python tools/release.py schluessel-erzeugen <datei.pem>
                                 NUR für den Herausgeber, lokal: privaten Schlüssel anlegen
                                 (Datei 0600, nie ins Repository) und den öffentlichen ausgeben
    python tools/release.py oeffentlich <datei.pem>  öffentlichen Schlüssel zu einem PEM ausgeben

Paketinhalt, Namen und Prüfung stehen in app/update_paket.py — derselbe Code,
mit dem das Relay ein Release vor dem Einspielen prüft.
"""
from __future__ import annotations

import base64
import os
import re
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import update_paket as up                                     # noqa: E402


def _fehler(text: str) -> int:
    print(f"FEHLER: {text}", file=sys.stderr)
    return 1


def kanal(tag: str) -> int:
    if not tag.startswith("v"):
        return _fehler(f"Tag {tag!r} beginnt nicht mit v")
    version = tag[1:]
    k = up.kanal_von(version)
    if k is None:
        return _fehler(f"Tag {tag!r}: erlaubt sind vX.Y.Z (stable) und vX.Y.Z-beta[.N] (beta)")
    datei = (WURZEL / "VERSION").read_text(encoding="utf-8").strip()
    if datei != version:
        return _fehler(f"Tag {tag} passt nicht zu VERSION ({datei})")
    print(f"version={version}")
    print(f"kanal={k}")
    return 0


def bauen(ziel: str) -> int:
    version = (WURZEL / "VERSION").read_text(encoding="utf-8").strip()
    pakete = up.bauen(WURZEL, Path(ziel), version)
    (Path(ziel) / up.SUMMEN).write_text(up.summen_schreiben(pakete), encoding="utf-8")
    for p in pakete:
        print(f"{p.name}  {p.stat().st_size} Byte")
    return 0


def notizen(version: str) -> int:
    text = (WURZEL / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(version)} .*?(?=^## |\Z)", text, re.M | re.S)
    if not m:
        return _fehler(f"Kein Abschnitt „## {version}“ in CHANGELOG.md")
    print(m.group(0).strip())
    print("\nPrüfen: `sha256sum -c SHA256SUMS`; die Signatur SHA256SUMS.sig (Ed25519) "
          "prüft das Relay vor dem Einspielen selbst.")
    return 0


def signieren(summen: str) -> int:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    pem = os.environ.get("RELEASE_SIGNING_KEY", "").strip()
    if not pem:
        return _fehler("RELEASE_SIGNING_KEY ist leer — ohne Signatur wird kein Release "
                       "veröffentlicht (README → Releases).")
    schluessel = load_pem_private_key(pem.encode(), password=None)
    if not isinstance(schluessel, Ed25519PrivateKey):
        return _fehler("RELEASE_SIGNING_KEY ist kein Ed25519-Schlüssel")
    pfad = Path(summen)
    sig = base64.b64encode(schluessel.sign(pfad.read_bytes())).decode()
    (pfad.parent / up.SIGNATUR).write_text(sig + "\n", encoding="ascii")
    print(f"{up.SIGNATUR} geschrieben")
    return 0


def pruefen(verzeichnis: str) -> int:
    d = Path(verzeichnis)
    summen, sig = d / up.SUMMEN, d / up.SIGNATUR
    if not sig.is_file():
        return _fehler(f"{up.SIGNATUR} fehlt")
    try:
        for name in up.summen_lesen(summen.read_bytes()):
            up.paket_pruefen(d / name, summen.read_bytes(), sig.read_bytes())
            print(f"OK {name}")
    except up.PaketFehler as exc:
        return _fehler(f"{exc} — passt der Schlüssel in RELEASE_SIGNING_KEY zu "
                       "RELEASE_SCHLUESSEL in app/update_paket.py?")
    return 0


def _oeffentlich_b64(schluessel) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return base64.b64encode(schluessel.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)).decode()


def schluessel_erzeugen(datei: str) -> int:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (Encoding, NoEncryption,
                                                              PrivateFormat)
    pfad = Path(datei).expanduser().resolve()
    if pfad.exists():
        return _fehler(f"{pfad} existiert schon — nicht überschrieben")
    try:
        pfad.relative_to(WURZEL)
        return _fehler("Der private Schlüssel gehört NICHT in das Repository")
    except ValueError:
        pass
    k = Ed25519PrivateKey.generate()
    fd = os.open(pfad, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    print(f"Privater Schlüssel: {pfad}")
    print(f"Öffentlicher Schlüssel (in RELEASE_SCHLUESSEL eintragen): {_oeffentlich_b64(k)}")
    return 0


def oeffentlich(datei: str) -> int:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    print(_oeffentlich_b64(load_pem_private_key(Path(datei).read_bytes(), password=None)))
    return 0


BEFEHLE = {"kanal": kanal, "bauen": bauen, "notizen": notizen, "signieren": signieren,
           "pruefen": pruefen, "schluessel-erzeugen": schluessel_erzeugen,
           "oeffentlich": oeffentlich}

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in BEFEHLE:
        print(__doc__)
        sys.exit(2)
    sys.exit(BEFEHLE[sys.argv[1]](sys.argv[2]))
