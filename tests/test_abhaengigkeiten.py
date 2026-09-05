"""requirements.txt (Absicht) und requirements.lock (Ergebnis) nennen dieselben Fassungen."""
import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"


def _pins(pfad: Path) -> dict[str, str]:
    aus = {}
    for zeile in pfad.read_text(encoding="utf-8").splitlines():
        zeile = zeile.split("#", 1)[0].strip()
        m = re.match(r"([A-Za-z0-9_.\-]+)==([^\s;]+)", zeile)
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
            if zeile:
                assert "==" in zeile and ">=" not in zeile, f"{pfad.name}: {zeile}"
