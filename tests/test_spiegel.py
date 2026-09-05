"""Gespiegelte Dateien sind inhaltsgleich mit dem Gateway — wenn es daneben liegt."""
import subprocess
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "tools"))

import driftcheck  # noqa: E402


def test_spiegelung():
    gw = driftcheck.gateway_finden(None)
    if gw is None:
        pytest.skip("kein Gateway-Baum daneben")
    rc = subprocess.run([sys.executable, str(WURZEL / "tools" / "driftcheck.py")],
                        capture_output=True, text=True).returncode
    assert rc == 0


def test_gespiegelte_dateien_existieren():
    for rel, _ in driftcheck.MIRRORED:
        assert (WURZEL / rel).is_file(), rel


def test_gespiegelte_module_importieren_nur_was_es_hier_gibt():
    """Die Kopien dürfen nichts aus dem Gateway brauchen, was hier fehlt."""
    sys.path.insert(0, str(WURZEL / "app"))
    import importlib
    for name in ("smtp_relay", "relay_hosts", "secure_io", "smtp_rauschen",
                 "mail_trace", "runtime_state", "login_drossel"):
        importlib.import_module(name)
