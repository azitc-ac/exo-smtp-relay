"""Release-Pakete des Relays — Inhalt, Namen, Prüfsummen, Signatur, Entpacken.

EINE Quelle für alle drei Seiten, die mit einem Release zu tun haben:

  * `tools/release_paket.py` baut daraus in der CI die Pakete und SHA256SUMS,
  * `aktualisierung.py` (Weboberfläche) prüft die heruntergeladenen Dateien,
    bevor es den Tausch anfordert,
  * `update_tausch.py` (läuft als root bzw. SYSTEM) prüft sie ein ZWEITES Mal,
    bevor es etwas austauscht — es vertraut dem Webprozess nicht, denn der
    schreibt in ein Verzeichnis, in das auch ein übernommener Dienst schreiben
    könnte.

Nur Standardbibliothek und `cryptography` (steht ohnehin in der Lock-Datei).
Kein Import aus der übrigen Anwendung: `update_tausch.py` lädt diese Datei aus
einer Arbeitskopie, während `app/` gerade ausgetauscht wird.

ZWEI WEGE DER ABSICHERUNG
-------------------------
1. SHA256SUMS nennt die SHA-256 jedes Pakets.
2. SHA256SUMS.sig ist eine Ed25519-Signatur über genau diese Datei (Base64,
   eine Zeile). Geprüft wird gegen die öffentlichen Schlüssel unten, die fest
   im Relay stehen. Wer Release-Assets auf GitHub austauscht, kann die
   Prüfsummen anpassen, aber nicht die Signatur.

Ohne eingebauten Schlüssel gilt JEDE Signatur als ungültig — dann wird nichts
eingespielt. Der private Schlüssel liegt nur bei der Release-CI (GitHub-Secret
RELEASE_SIGNING_KEY); wie er entsteht, steht in der README unter „Releases“.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

# ── Öffentliche Prüfschlüssel (Ed25519, 32 Byte roh, Base64) ─────────────────
# Angelegt am 24.11.2026 (README → Releases). Mehrere
# Einträge erlauben einen Schlüsselwechsel: neuen dazunehmen, ein Release mit
# beiden ausliefern, dann den alten streichen.
RELEASE_SCHLUESSEL: tuple[str, ...] = (
    "BW4h8umUvD5H4PZ6k5E3qIITyEUhoP3gQQUaLYLZD84=",  # Hauptschlüssel (GitHub-Secret RELEASE_SIGNING_KEY)
    "AvB4kdUIcqtnvJLftPOGpRcsLjnJffrCGiJVIdkDEvk=",  # Reserve, offline verwahrt – für den Fall, dass der Hauptschlüssel verloren geht
)

NAME = "exo-smtp-relay"

# Was ein Paket enthält und was der Tausch ersetzt. `data/` und `venv/` stehen
# NIE darin: Daten bleiben unberührt, die venv wird nur über pip angepasst.
# LICENSE.md und NOTICE sind Pflicht (Apache-2.0, Abschnitt 4).
PAKET_INHALT: tuple[str, ...] = (
    "app", "windows", "linux",
    "VERSION", "CHANGELOG.md", "LICENSE.md", "NOTICE", "README.md",
)
PFLICHT = ("app/main.py", "app/requirements.lock", "VERSION", "LICENSE.md", "NOTICE")

SUMMEN = "SHA256SUMS"
SIGNATUR = "SHA256SUMS.sig"
ART_ENDUNG = {"windows": "windows.zip", "linux": "linux.tar.gz"}

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-beta(?:\.(\d+))?)?$")
_PAKET_RE = re.compile(r"^exo-smtp-relay-(\d+\.\d+\.\d+(?:-beta(?:\.\d+)?)?)-(windows\.zip|linux\.tar\.gz)$")
# Obergrenze für ein entpacktes Paket — schützt vor Archivbomben.
MAX_ENTPACKT = 200 * 1024 * 1024


class PaketFehler(ValueError):
    """Paket, Prüfsumme oder Signatur ungültig — nichts einspielen."""


# ── Versionen und Kanäle ─────────────────────────────────────────────────────

def version_schluessel(v: str) -> tuple | None:
    """Sortierschlüssel: 0.2.40-beta.1 < 0.2.40-beta.2 < 0.2.40 < 0.2.41.
    None für alles, was kein Release-Format hat."""
    m = _VERSION_RE.match((v or "").strip().lstrip("v"))
    if not m:
        return None
    a, b, c, beta = m.groups()
    ist_beta = "-beta" in v
    return (int(a), int(b), int(c), 0 if ist_beta else 1, int(beta or 0))


def kanal_von(v: str) -> str | None:
    """Kanal aus der Version bzw. dem Tag: ohne Suffix stable, -beta[.N] beta."""
    if version_schluessel(v) is None:
        return None
    return "beta" if "-beta" in v else "stable"


def paket_name(version: str, art: str) -> str:
    return f"{NAME}-{version}-{ART_ENDUNG[art]}"


def paket_name_gueltig(name: str) -> bool:
    return bool(_PAKET_RE.match(name or ""))


# ── Prüfsummen und Signatur ──────────────────────────────────────────────────

def summen_lesen(text: bytes | str) -> dict[str, str]:
    """SHA256SUMS (Format von sha256sum) → {Dateiname: hex}."""
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    aus: dict[str, str] = {}
    for zeile in text.splitlines():
        if not zeile.strip():
            continue
        m = re.match(r"^([0-9a-f]{64}) [ *](\S+)$", zeile.strip())
        if not m:
            raise PaketFehler(f"SHA256SUMS: Zeile unlesbar: {zeile[:80]!r}")
        aus[m.group(2)] = m.group(1)
    return aus


def summen_schreiben(dateien: list[Path]) -> str:
    return "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
                   for p in sorted(dateien, key=lambda p: p.name))


def signatur_pruefen(summen: bytes, signatur: bytes,
                     schluessel: tuple[str, ...] | None = None) -> None:
    """Ed25519-Signatur über SHA256SUMS prüfen. Wirft PaketFehler."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    schluessel = RELEASE_SCHLUESSEL if schluessel is None else schluessel
    if not schluessel:
        raise PaketFehler("Kein Prüfschlüssel eingebaut — ohne gültige Signatur "
                          "wird nichts eingespielt.")
    try:
        sig = base64.b64decode(signatur.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise PaketFehler("Signatur ist kein gültiges Base64.")
    if len(sig) != 64:
        raise PaketFehler("Signatur hat nicht die Länge einer Ed25519-Signatur.")
    for b64 in schluessel:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(b64)).verify(sig, summen)
            return
        except InvalidSignature:
            continue
    raise PaketFehler("Signatur der Prüfsummen ungültig — Release nicht vom "
                      "Herausgeber signiert oder verändert.")


def paket_pruefen(paket: Path, summen: bytes, signatur: bytes,
                  schluessel: tuple[str, ...] | None = None) -> str:
    """Signatur UND Prüfsumme eines Pakets prüfen. Rückgabe: Version im Namen."""
    signatur_pruefen(summen, signatur, schluessel)
    m = _PAKET_RE.match(paket.name)
    if not m:
        raise PaketFehler(f"Unerwarteter Paketname {paket.name!r}.")
    soll = summen_lesen(summen).get(paket.name)
    if not soll:
        raise PaketFehler(f"{paket.name} steht nicht in SHA256SUMS.")
    ist = hashlib.sha256(paket.read_bytes()).hexdigest()
    if ist != soll:
        raise PaketFehler(f"Prüfsumme von {paket.name} stimmt nicht "
                          f"(erwartet {soll[:12]}…, erhalten {ist[:12]}…).")
    return m.group(1)


# ── Entpacken ────────────────────────────────────────────────────────────────

def _sicherer_name(name: str, wurzel: str) -> PurePosixPath | None:
    """Pfad innerhalb des Pakets relativ zum Kopfordner — oder PaketFehler."""
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise PaketFehler(f"Unzulässiger Pfad im Paket: {name!r}")
    p = PurePosixPath(name)
    if ".." in p.parts:
        raise PaketFehler(f"Unzulässiger Pfad im Paket: {name!r}")
    if not p.parts or p.parts[0] != wurzel:
        raise PaketFehler(f"Datei ausserhalb von {wurzel}/: {name!r}")
    rest = PurePosixPath(*p.parts[1:]) if len(p.parts) > 1 else None
    if rest is not None and rest.parts[0] not in PAKET_INHALT:
        raise PaketFehler(f"Unerwarteter Eintrag im Paket: {name!r}")
    return rest


def entpacken(paket: Path, ziel: Path, version: str) -> None:
    """Paket nach `ziel` entpacken (Inhalt des Kopfordners). Nur reguläre
    Dateien und Verzeichnisse; keine Links, kein `..`, nichts ausserhalb der
    Paketliste. Prüft danach Pflichtdateien und die Version."""
    wurzel = f"{NAME}-{version}"
    ziel.mkdir(parents=True, exist_ok=False)
    gesamt = 0
    if paket.name.endswith(".zip"):
        with zipfile.ZipFile(paket) as z:
            for info in z.infolist():
                rest = _sicherer_name(info.filename.rstrip("/"), wurzel)
                if rest is None:
                    continue
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise PaketFehler(f"Verknüpfung im Paket: {info.filename!r}")
                if info.is_dir():
                    (ziel / rest).mkdir(parents=True, exist_ok=True)
                    continue
                gesamt += info.file_size
                if gesamt > MAX_ENTPACKT:
                    raise PaketFehler("Paket entpackt zu gross.")
                (ziel / rest).parent.mkdir(parents=True, exist_ok=True)
                (ziel / rest).write_bytes(z.read(info))
    else:
        with tarfile.open(paket, "r:gz") as t:
            for info in t.getmembers():
                rest = _sicherer_name(info.name.rstrip("/"), wurzel)
                if rest is None:
                    continue
                if info.isdir():
                    (ziel / rest).mkdir(parents=True, exist_ok=True)
                    continue
                if not info.isfile():
                    raise PaketFehler(f"Keine reguläre Datei im Paket: {info.name!r}")
                gesamt += info.size
                if gesamt > MAX_ENTPACKT:
                    raise PaketFehler("Paket entpackt zu gross.")
                (ziel / rest).parent.mkdir(parents=True, exist_ok=True)
                quelle = t.extractfile(info)
                (ziel / rest).write_bytes(quelle.read() if quelle else b"")
                if info.mode & 0o111:
                    (ziel / rest).chmod(0o755)
    for pflicht in PFLICHT:
        if not (ziel / pflicht).is_file():
            raise PaketFehler(f"Pflichtdatei fehlt im Paket: {pflicht}")
    drin = (ziel / "VERSION").read_text(encoding="utf-8").strip()
    if drin != version:
        raise PaketFehler(f"Paket trägt Version {drin}, erwartet {version}.")


# ── Bauen (CI: tools/release_paket.py) ───────────────────────────────────────

def _dateien(quelle: Path):
    for teil in PAKET_INHALT:
        p = quelle / teil
        if p.is_file():
            yield p, teil
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts and f.suffix not in (".pyc", ".pyo"):
                    yield f, f.relative_to(quelle).as_posix()


def bauen(quelle: Path, ziel: Path, version: str) -> list[Path]:
    """Windows-ZIP und Linux-Archiv aus dem Baum `quelle` bauen (gleicher
    Inhalt, Kopfordner exo-smtp-relay-<version>/). Feste Zeitstempel, damit
    zweimal Bauen dieselben Bytes ergibt."""
    for pflicht in PFLICHT:
        if not (quelle / pflicht).is_file():
            raise PaketFehler(f"Pflichtdatei fehlt im Baum: {pflicht}")
    if (quelle / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise PaketFehler("VERSION im Baum passt nicht zur Release-Version.")
    ziel.mkdir(parents=True, exist_ok=True)
    kopf = f"{NAME}-{version}"
    dateien = list(_dateien(quelle))

    zip_pfad = ziel / paket_name(version, "windows")
    with zipfile.ZipFile(zip_pfad, "w", zipfile.ZIP_DEFLATED) as z:
        for f, rel in dateien:
            info = zipfile.ZipInfo(f"{kopf}/{rel}", date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())

    tar_pfad = ziel / paket_name(version, "linux")
    puffer = io.BytesIO()
    with tarfile.open(fileobj=puffer, mode="w", format=tarfile.PAX_FORMAT) as t:
        for f, rel in dateien:
            daten = f.read_bytes()
            info = tarfile.TarInfo(f"{kopf}/{rel}")
            info.size, info.mtime, info.uid, info.gid = len(daten), 1577836800, 0, 0
            info.mode = 0o755 if f.suffix in (".sh",) else 0o644
            t.addfile(info, io.BytesIO(daten))
    import gzip
    with open(tar_pfad, "wb") as roh, gzip.GzipFile(fileobj=roh, mode="wb", mtime=0,
                                                       filename="") as gz:
        gz.write(puffer.getvalue())
    return [zip_pfad, tar_pfad]
