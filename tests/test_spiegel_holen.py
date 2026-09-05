"""spiegel_holen: kopiert nur in Richtung Gateway → Relay, und nie über eine
Relay-Änderung hinweg."""
import subprocess
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "tools"))

import driftcheck  # noqa: E402
import spiegel_holen  # noqa: E402


def _gateway_attrappe(tmp_path: Path) -> Path:
    """Ein Gateway-Baum, in dem eine gespiegelte Datei abweicht."""
    gw = tmp_path / "EXO-Signature-Gateway"
    for rel, _ in driftcheck.MIRRORED:
        ziel = gw / rel
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes((WURZEL / rel).read_bytes())
    (gw / "app" / "handler.py").write_text("# Attrappe\n")
    (gw / "Dockerfile").write_text("FROM scratch\n")
    (gw / "app" / "smtp_relay.py").write_text(
        (WURZEL / "app" / "smtp_relay.py").read_text(encoding="utf-8") + "\n# Neue Regel im Gateway\n",
        encoding="utf-8")
    return gw


def test_zeigt_abweichung_ohne_zu_aendern(tmp_path):
    gw = _gateway_attrappe(tmp_path)
    liste = spiegel_holen.abweichungen(gw)
    assert [e["rel"] for e in liste] == ["app/smtp_relay.py"]
    assert liste[0]["relay_neuer"] is False, "ohne git-Datum gilt das Gateway als Quelle"
    r = subprocess.run([sys.executable, str(WURZEL / "tools" / "spiegel_holen.py"), "--gateway", str(gw)],
                       capture_output=True, text=True)
    assert r.returncode == 1
    assert "ABWEICHUNG  app/smtp_relay.py" in r.stdout
    assert "--uebernehmen" in r.stdout
    assert "Neue Regel im Gateway" not in (WURZEL / "app" / "smtp_relay.py").read_text(encoding="utf-8")


def test_uebernehmen_kopiert_nur_die_abweichende_datei(tmp_path, monkeypatch):
    """Gegen eine Kopie des Relay-Baums, damit die echte Datei unangetastet bleibt."""
    gw = _gateway_attrappe(tmp_path)
    relay_kopie = tmp_path / "relay"
    for rel, _ in driftcheck.MIRRORED:
        (relay_kopie / rel).parent.mkdir(parents=True, exist_ok=True)
        (relay_kopie / rel).write_bytes((WURZEL / rel).read_bytes())
    monkeypatch.setattr(spiegel_holen, "RELAY", relay_kopie)
    monkeypatch.setattr(sys, "argv", ["spiegel_holen", "--gateway", str(gw), "--uebernehmen"])
    assert spiegel_holen.main() == 0
    assert "Neue Regel im Gateway" in (relay_kopie / "app" / "smtp_relay.py").read_text(encoding="utf-8")
    for rel, _ in driftcheck.MIRRORED:
        if rel != "app/smtp_relay.py":
            assert (relay_kopie / rel).read_bytes() == (WURZEL / rel).read_bytes()


def test_relay_neuer_wird_nicht_ueberschrieben(tmp_path, monkeypatch):
    gw = _gateway_attrappe(tmp_path)
    relay_kopie = tmp_path / "relay"
    for rel, _ in driftcheck.MIRRORED:
        (relay_kopie / rel).parent.mkdir(parents=True, exist_ok=True)
        (relay_kopie / rel).write_bytes((WURZEL / rel).read_bytes())
    monkeypatch.setattr(spiegel_holen, "RELAY", relay_kopie)
    # Das Relay hat die Datei später berührt als das Gateway.
    monkeypatch.setattr(spiegel_holen, "_datum",
                        lambda repo, rel: "2026-09-05T00:00:00+00:00" if repo == relay_kopie else "2026-09-01T00:00:00+00:00")
    monkeypatch.setattr(sys, "argv", ["spiegel_holen", "--gateway", str(gw), "--uebernehmen"])
    assert spiegel_holen.main() == 1
    assert "Neue Regel im Gateway" not in (relay_kopie / "app" / "smtp_relay.py").read_text(encoding="utf-8")
