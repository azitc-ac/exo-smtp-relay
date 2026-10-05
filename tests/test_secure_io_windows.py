"""Unter Windows gibt es keine Unix-Modi: harden_tree darf nicht bei jedem Start "korrigieren"."""
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))


def test_windows_haertet_nicht_per_chmod(tmp_path, monkeypatch):
    import secure_io
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "key.pem").write_text("x")
    monkeypatch.setattr(secure_io.sys, "platform", "win32")
    assert secure_io.harden_tree(tmp_path) == {"files": 0, "dirs": 0}
