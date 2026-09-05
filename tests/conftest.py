"""Gemeinsame Vorbereitung der Testsuite.

⚠️ DATA_DIR wird gesetzt, BEVOR irgendein Anwendungsmodul importiert wird: Die
Module lesen `config.DATA_DIR` beim Import und legen daraus ihre Pfade fest.
Ein Wegwerf-Verzeichnis, das es ausserhalb des Laufs nicht gibt, kann niemand
versehentlich treffen (im Gateway am 26.07.2026 beinahe passiert).
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

_TEST_DATA = Path(tempfile.mkdtemp(prefix="exo-relay-tests-"))
os.environ["DATA_DIR"] = str(_TEST_DATA)
os.environ.setdefault("PWSH", "pwsh-gibt-es-im-test-nicht")

WURZEL = Path(__file__).resolve().parent.parent
APP = WURZEL / "app"
sys.path.insert(0, str(APP))

# Das grosse Gateway, wenn es daneben liegt (Monorepo-Phase: ../app; eigenes
# Repo: ../EXO-Signature-Gateway/app). Für die Spiegelprüfung.
GATEWAY_KANDIDATEN = (WURZEL.parent / "app", WURZEL.parent / "EXO-Signature-Gateway" / "app")


def gateway_app() -> Path | None:
    for k in GATEWAY_KANDIDATEN:
        if (k / "smtp_relay.py").is_file():
            return k
    return None


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    yield d
    for p in sorted(d.rglob("*"), reverse=True):
        try:
            p.chmod(0o700 if p.is_dir() else 0o600)
        except OSError:
            pass


@pytest.fixture
def einstellungen(monkeypatch):
    """Einstellungen als Wörterbuch — `settings_store.get` liest daraus."""
    werte = {
        "SMTP_RELAY_ENABLED": True,
        "SMTP_RELAY_LERN_NETZE": [],
        "SMTP_RELAY_LERN_BIS": "",
        "SMTP_RELAY_EXTERN_VORGABE": False,
        "REINJECT_MODE": "smtp",
        "TENANT_DOMAIN": "firma.onmicrosoft.com",
        "PUBLIC_HOSTNAME": "relay.firma.de",
        "EXO_SUBMIT_MODE": "smarthost",
        "EXO_SMARTHOST": "firma-de.mail.protection.outlook.com",
        "ADRESSEN_ZUSAETZLICH": [],
    }
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    monkeypatch.setattr(settings_store, "update", lambda patch: werte.update(patch))
    return werte
