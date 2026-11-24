"""Postfachliste: „abgelaufen“ ist -inf, nicht 0.0 (v0.2.32, wie Gateway v1.12.68).

time.monotonic() zählt ab Systemstart. In der ersten Stunde nach einem
Neustart (Pi, CI-Runner) galt 0.0 als frisch: invalidate() wirkte nicht, und
ein von der Platte geladener Stand wurde nicht sofort nachgeladen.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import exo_mailboxes  # noqa: E402

ALT = [{"guid": "g", "primary": "c@firma.de", "addresses": ["c@firma.de"],
        "display_name": "", "type": "UserMailbox"}]


def _vorbereiten(monkeypatch, tmp_path):
    monkeypatch.setattr(exo_mailboxes.time, "monotonic", lambda: 120.0)   # zwei Minuten nach dem Start
    monkeypatch.setattr(exo_mailboxes, "CACHE_PATH", tmp_path / "mailboxes.json")
    monkeypatch.setattr(exo_mailboxes, "_cache_schreiben", lambda: None)
    abrufe = []
    monkeypatch.setattr(exo_mailboxes, "fetch_mailboxes", lambda: abrufe.append(1) or list(ALT))
    return abrufe


def test_invalidate_wirkt_kurz_nach_systemstart(monkeypatch, tmp_path):
    abrufe = _vorbereiten(monkeypatch, tmp_path)
    monkeypatch.setattr(exo_mailboxes, "_cache", list(ALT))
    monkeypatch.setattr(exo_mailboxes, "_cache_ts", 100.0)
    exo_mailboxes.list_mailboxes()
    assert abrufe == [], "frischer Stand: kein Abruf"
    exo_mailboxes.invalidate()
    exo_mailboxes.list_mailboxes()
    assert len(abrufe) == 1, "nach invalidate muss abgerufen werden, auch kurz nach dem Systemstart"


def test_stand_von_der_platte_wird_kurz_nach_systemstart_nachgeladen(monkeypatch, tmp_path):
    abrufe = _vorbereiten(monkeypatch, tmp_path)
    (tmp_path / "mailboxes.json").write_text(json.dumps({"mailboxes": ALT}), encoding="utf-8")
    monkeypatch.setattr(exo_mailboxes, "_cache", [])
    exo_mailboxes.list_mailboxes()
    assert len(abrufe) == 1, "der Stand von der Platte gilt als alt"
