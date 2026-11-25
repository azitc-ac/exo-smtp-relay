"""requirements.txt (Absicht) und requirements.lock (Ergebnis) nennen dieselben Fassungen.

Seit 0.2.47 trägt die Lock-Datei zu jeder Anforderung die SHA-256 ihrer Wheels, und JEDE
Stelle, die aus ihr installiert, tut das mit `--require-hashes` (Lieferkette: beim Kunden
wird nichts installiert, dessen Prüfsumme nicht hier steht).
"""
import re
import sys
from pathlib import Path

import pytest

from conftest import WURZEL, gateway_app

APP = WURZEL / "app"
sys.path.insert(0, str(WURZEL / "tools"))
import lock_hashes  # noqa: E402

_ANF = re.compile(r"([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s;\\]+)")


def _pins(pfad: Path) -> dict[str, str]:
    aus = {}
    for zeile in pfad.read_text(encoding="utf-8").splitlines():
        zeile = zeile.split("#", 1)[0].strip()
        m = _ANF.match(zeile)
        if m:
            aus[m.group(1).lower().replace("_", "-")] = m.group(2)
    return aus


def test_lock_enthaelt_alle_direkten_pakete_in_derselben_fassung():
    txt, lock = _pins(APP / "requirements.txt"), _pins(APP / "requirements.lock")
    for paket, fassung in txt.items():
        assert paket in lock, f"{paket} fehlt in requirements.lock"
        assert lock[paket] == fassung, f"{paket}: txt {fassung} ≠ lock {lock[paket]}"


def test_alles_exakt_gepinnt():
    for pfad in (APP / "requirements.txt", APP / "requirements.lock"):
        for zeile in pfad.read_text(encoding="utf-8").splitlines():
            zeile = zeile.split("#", 1)[0].strip()
            if zeile and not zeile.startswith("--hash=sha256:"):
                assert "==" in zeile and ">=" not in zeile, f"{pfad.name}: {zeile}"


def test_jede_anforderung_traegt_pruefsummen():
    """Hash-Modus ist alles oder nichts: fehlt EINE Prüfsumme, installiert pip gar nichts."""
    eintraege = lock_hashes.eintraege((APP / "requirements.lock").read_text(encoding="utf-8"))
    assert len(eintraege) == len(_pins(APP / "requirements.lock"))
    for e in eintraege:
        assert e["hashes"], f"{e['name']}: keine --hash-Zeile"
        assert len(set(e["hashes"])) == len(e["hashes"]), f"{e['name']}: doppelte Prüfsumme"


def test_extras_der_abhaengigkeiten_stehen_in_der_lock():
    """msal fordert PyJWT[crypto]. Ohne das Extra in der Lock-Datei bricht pip im
    Hash-Modus ab (auf dem Raspberry Pi gemessen am 24.11.2026)."""
    text = (APP / "requirements.lock").read_text(encoding="utf-8")
    assert re.search(r"^PyJWT\[crypto\]==", text, re.M)


def test_wheel_auswahl():
    p = lock_hashes.passt
    assert p("cryptography-50.0.0-cp311-abi3-win_amd64.whl")
    assert p("cryptography-50.0.0-cp39-abi3-manylinux_2_34_aarch64.whl")
    assert p("pydantic_core-2.46.4-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl")
    assert p("idna-3.18-py3-none-any.whl")
    assert not p("cryptography-50.0.0-cp314-cp314t-win_amd64.whl")          # free-threaded
    assert not p("cryptography-50.0.0-cp311-abi3-musllinux_1_2_x86_64.whl")
    assert not p("cryptography-50.0.0-cp311-abi3-macosx_11_0_arm64.whl")
    assert not p("pydantic_core-2.46.4-cp310-cp310-win_amd64.whl")
    assert not p("cryptography-50.0.0.tar.gz")                               # kein Quellpaket
    assert p("pywin32-311-cp312-cp312-win_amd64.whl", "win_amd64", "312")
    assert not p("pywin32-311-cp312-cp312-win_amd64.whl", "win_amd64", "313")


def test_lock_neu_schreiben_ist_stabil():
    """Werkzeug-Rundlauf ohne Netz: Einträge lesen und mit denselben Prüfsummen neu
    schreiben ergibt dieselbe Datei (Kommentare, Reihenfolge, Extras, Marken bleiben)."""
    text = (APP / "requirements.lock").read_text(encoding="utf-8")
    hashes = {lock_hashes._norm(e["name"]): e["hashes"] for e in lock_hashes.eintraege(text)}
    assert lock_hashes.neu_schreiben(text, hashes) == text


# Jede Stelle, die aus der Lock-Datei installiert. Findet der Suchlauf unten eine weitere,
# schlägt test_jede_installation_prueft_hashes fehl, bis sie hier steht UND die Prüfung hat.
# install.ps1 und update_tausch bauen ihre Argumente in EINER Funktion (test_offline_paket).
_INSTALLATIONEN = {
    "Dockerfile", "linux/exo-smtp-relay.service", "README.md", "tools/lock_hashes.py",
    ".github/workflows/ci.yml", ".github/workflows/spiegel.yml", ".github/workflows/release.yml",
}


def _installationszeilen():
    for pfad in sorted(WURZEL.rglob("*")):
        if not pfad.is_file() or ".git" in pfad.parts or "__pycache__" in pfad.parts \
                or pfad.suffix not in ("", ".ps1", ".yml", ".md", ".service", ".sh", ".py") \
                or pfad.name == "CHANGELOG.md" or "tests" in pfad.parts:
            continue
        try:
            text = pfad.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        logisch = text.replace("\\\n", " ").replace("`\n", " ")
        for zeile in logisch.splitlines():
            if re.search(r"\bpip(3)?\b.*\binstall\b", zeile) and "requirements.lock" in zeile \
                    and not zeile.lstrip().startswith(("pip-audit", "#   (")) and "@pipArgs" not in zeile:
                yield pfad.relative_to(WURZEL).as_posix(), zeile.strip()


def test_jede_installation_prueft_hashes():
    gefunden = set()
    for rel, zeile in _installationszeilen():
        gefunden.add(rel)
        assert "--require-hashes" in zeile and re.search(r'--only-binary "?:all:"?', zeile), f"{rel}: {zeile}"
    assert gefunden == _INSTALLATIONEN, f"Installationsstellen: {sorted(gefunden)}"


def test_tauscher_installiert_mit_hashes(tmp_path, monkeypatch):
    """update_tausch.phase_pip: online --require-hashes, offline zusätzlich --no-index
    mit den Wheels aus offline/wheels — nie PyPI."""
    import update_tausch
    aufrufe = []

    class _R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(update_tausch.subprocess, "run", lambda args, **kw: aufrufe.append(args) or _R())
    py = update_tausch.venv_python(tmp_path)
    py.parent.mkdir(parents=True)
    py.write_text("")
    update_tausch.phase_pip(tmp_path)
    online = aufrufe[-1]
    assert "--require-hashes" in online and "--only-binary" in online
    assert "--no-index" not in online

    (tmp_path / "offline" / "wheels").mkdir(parents=True)
    (tmp_path / "offline" / "inhalt.json").write_text("{}")
    assert "offline" in update_tausch.phase_pip(tmp_path).lower()
    offline = aufrufe[-1]
    assert "--require-hashes" in offline and "--no-index" in offline
    assert offline[offline.index("--find-links") + 1] == str(tmp_path / "offline" / "wheels")


def test_fassungen_wie_im_gateway():
    """Kopf der Lock-Datei: dieselben Fassungen wie im Gateway. Nur wenn es daneben liegt."""
    gw = gateway_app()
    if gw is None or not (gw / "requirements.lock").is_file():
        pytest.skip("Gateway liegt nicht daneben")
    relay, gateway = _pins(APP / "requirements.lock"), _pins(gw / "requirements.lock")
    abweichend = {p: (v, gateway[p]) for p, v in relay.items() if p in gateway and gateway[p] != v}
    assert not abweichend, f"Relay ≠ Gateway: {abweichend}"
