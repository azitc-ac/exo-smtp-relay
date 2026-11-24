"""Labtest 0.2.43 (Windows Server 2025): zwei Upgrade-Befunde.

1. install.ps1 meldete nach einem UPGRADE weiter „admin / admin - bitte aendern“,
   obwohl das Kennwort längst geändert war. Jetzt fragt der Installer dieselbe
   Regel wie die Oberfläche (`app/kennwort_stand.py`).
2. data\\logs\\service.log (stdout/stderr des Kindprozesses, geschrieben vom
   Dienst-Wrapper windows/service.py) stand in cp1252 („�bersprungen“), app.log
   in UTF-8. Der Wrapper setzt jetzt PYTHONIOENCODING=utf-8 für das Kind.
"""
import ast
import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from conftest import APP, WURZEL

STAND = APP / "kennwort_stand.py"


# ── 1. Kennwort-Hinweis ────────────────────────────────────────────────────────

def _stand(settings: Path, **env) -> subprocess.CompletedProcess:
    umgebung = {k: v for k, v in os.environ.items() if k != "WEBUI_PASSWORD"}
    umgebung.update(env)
    return subprocess.run([sys.executable, str(STAND), str(settings)],
                          capture_output=True, text=True, env=umgebung)


def test_eigenes_kennwort_gesetzt(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"ADMIN_PASSWORD_HASH": "salt$abc"}), encoding="utf-8")
    r = _stand(s)
    assert (r.returncode, r.stdout.strip(), r.stderr) == (0, "eigenes", "")


@pytest.mark.parametrize("inhalt", [None, {"ADMIN_PASSWORD_HASH": ""}, {"ANDERES": 1}])
def test_noch_vorgabe(tmp_path, inhalt):
    s = tmp_path / "settings.json"
    if inhalt is not None:
        s.write_text(json.dumps(inhalt), encoding="utf-8")
    r = _stand(s)
    assert (r.returncode, r.stdout.strip()) == (0, "vorgabe")


def test_eigenes_kennwort_aus_umgebung(tmp_path):
    assert _stand(tmp_path / "fehlt.json", WEBUI_PASSWORD="sehr-eigen").stdout.strip() == "eigenes"
    assert _stand(tmp_path / "fehlt.json", WEBUI_PASSWORD="changeme").stdout.strip() == "vorgabe"


def test_unlesbar_meldet_unbekannt_ohne_stderr(tmp_path):
    """PowerShell 5.1 bricht bei stderr eines Programms unter ErrorActionPreference=Stop ab."""
    s = tmp_path / "settings.json"
    s.write_text("{kaputt", encoding="utf-8")
    r = _stand(s)
    assert (r.returncode, r.stdout.strip(), r.stderr) == (0, "unbekannt", "")


def test_kennwort_stand_nur_standardbibliothek():
    """install.ps1 ruft es direkt mit dem venv-Python auf — keine Anwendungsmodule."""
    baum = ast.parse(STAND.read_text(encoding="utf-8"))
    module = {a.name.split(".")[0] for k in ast.walk(baum) if isinstance(k, ast.Import) for a in k.names}
    module |= {k.module.split(".")[0] for k in ast.walk(baum) if isinstance(k, ast.ImportFrom)}
    assert module <= {"__future__", "json", "os", "sys", "pathlib"}, module


def test_oberflaeche_und_installer_teilen_eine_regel(monkeypatch):
    from webui import deps
    import kennwort_stand
    aufrufe = []
    monkeypatch.setattr(kennwort_stand, "aenderung_noetig", lambda h, p: aufrufe.append((h, p)) or True)
    monkeypatch.setattr(deps.settings_store, "get", lambda k, *a, **kw: "h" if k == "ADMIN_PASSWORD_HASH" else None)
    assert deps._password_change_required() is True and aufrufe == [("h", deps.config.WEBUI_PASSWORD)]


def test_install_ps1_meldet_kennwort_nach_regel():
    ps = (WURZEL / "windows" / "install.ps1").read_text(encoding="utf-8-sig")
    assert "app\\kennwort_stand.py" in ps
    schluss = [z for z in ps.splitlines() if "Weboberflaeche: https://" in z]
    assert len(schluss) == 1, schluss
    assert "$kennwortHinweis" in schluss[0] and "admin / admin" not in schluss[0], schluss[0]
    # „admin / admin“ nur noch im Zweig für das Vorgabe-Kennwort.
    zweige = [z for z in ps.splitlines() if "admin / admin - bitte aendern" in z]
    assert zweige == ['    $kennwortHinweis = "admin / admin - bitte aendern"'], zweige


# ── 2. service.log in UTF-8 ────────────────────────────────────────────────────

def _service_modul(monkeypatch):
    for name in ("servicemanager", "win32event", "win32service"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    su = types.ModuleType("win32serviceutil")
    su.ServiceFramework = type("ServiceFramework", (), {"__init__": lambda self, args: None})
    monkeypatch.setitem(sys.modules, "win32serviceutil", su)
    spec = importlib.util.spec_from_file_location("_relay_service", WURZEL / "windows" / "service.py")
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def test_service_log_ist_utf8_auch_bei_ansi_codepage(tmp_path, monkeypatch):
    service = _service_modul(monkeypatch)
    app = tmp_path / "app"
    app.mkdir()
    # Wie main.py: basicConfig schreibt auf stderr, das der Wrapper in service.log umleitet.
    (app / "main.py").write_text(
        "import logging\nlogging.basicConfig(format='%(message)s')\n"
        "logging.getLogger('t').warning('EXO-Postfachliste übersprungen — Grund')\n",
        encoding="utf-8")
    monkeypatch.setattr(service, "APP", app)
    monkeypatch.setattr(service, "VENV", tmp_path / "keine-venv")
    monkeypatch.setattr(service, "DATA", tmp_path / "data")
    monkeypatch.setattr(service, "LOGDATEI", tmp_path / "data" / "logs" / "service.log")
    # Ohne Konsole nimmt Python unter Windows die ANSI-Codepage — hier nachgestellt.
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    proc = service.RelayService.__new__(service.RelayService)._starten()
    assert proc.wait(timeout=60) == 0
    roh = (tmp_path / "data" / "logs" / "service.log").read_bytes()
    assert "übersprungen — Grund".encode("utf-8") in roh, roh


# ── 3. Dienst-Umgebung beim Upgrade zusammenführen, Port behalten ──────────────
# Bestehende Installationen ohne nachgeladene Wurzeln MÜSSEN für 0.2.43+ einmal
# install.ps1 ausführen — dieser Weg darf weder Port noch eigene Einträge verlieren.

def _umgebung():
    spec = importlib.util.spec_from_file_location("_dienst_umgebung",
                                                  WURZEL / "windows" / "dienst_umgebung.py")
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def _lauf(alt, *args):
    du = _umgebung()
    geschrieben = []
    rc = du.main(["dienst_umgebung.py", "--setzen", "PYTHONPATH=C:\\neu\\sp", *args],
                 lesen=lambda: list(alt), schreiben=geschrieben.append)
    return rc, geschrieben


def test_upgrade_behaelt_eigene_eintraege_und_port(capsys):
    alt = ["PYTHONPATH=C:\\alt\\sp", "WEBUI_PORT=9443", "WEBUI_PASSWORD=geheim", "EIGENE=1"]
    rc, geschrieben = _lauf(alt)
    assert (rc, capsys.readouterr().out.strip()) == (0, "9443")
    assert geschrieben == [["PYTHONPATH=C:\\neu\\sp", "WEBUI_PORT=9443",
                            "WEBUI_PASSWORD=geheim", "EIGENE=1"]]


def test_ausdruecklicher_port_gewinnt(capsys):
    rc, geschrieben = _lauf(["WEBUI_PORT=9443", "WEBUI_PASSWORD=geheim"], "--port", "10443")
    assert (rc, capsys.readouterr().out.strip()) == (0, "10443")
    assert geschrieben == [["WEBUI_PORT=10443", "WEBUI_PASSWORD=geheim", "PYTHONPATH=C:\\neu\\sp"]]


def test_erstinstallation_8443(capsys):
    rc, geschrieben = _lauf([])
    assert (rc, capsys.readouterr().out.strip()) == (0, "8443")
    assert geschrieben == [["PYTHONPATH=C:\\neu\\sp", "WEBUI_PORT=8443"]]


def test_doppelte_und_klein_geschriebene_installer_schluessel(capsys):
    rc, geschrieben = _lauf(["webui_port=7443", "Pythonpath=x", "PYTHONPATH=y", "A=b=c"])
    assert rc == 0 and capsys.readouterr().out.strip() == "7443"
    assert geschrieben == [["WEBUI_PORT=7443", "PYTHONPATH=C:\\neu\\sp", "A=b=c"]]


def test_fehler_ohne_schreiben_und_ohne_stderr(capsys):
    rc, geschrieben = _lauf(["WEBUI_PORT=9443"], "--port", "99999")
    aus = capsys.readouterr()
    assert rc == 1 and geschrieben == [] and aus.err == "" and "Fehler" in aus.out


def test_install_ps1_fuehrt_umgebung_zusammen():
    ps = (WURZEL / "windows" / "install.ps1").read_text(encoding="utf-8-sig")
    code = "\n".join(z for z in ps.splitlines() if not z.lstrip().startswith("#"))
    assert "windows\\dienst_umgebung.py" in code
    # Kein direktes Überschreiben des Registry-Werts mehr.
    assert '-Name "Environment"' not in code and "Set-ItemProperty" not in code
    # Port nur bei ausdrücklichem -WebPort weiterreichen, danach den wirksamen übernehmen.
    assert '$PSBoundParameters.ContainsKey("WebPort")' in code
    assert "$WebPort = [int]($umgebungAusgabe[-1])" in code
    assert "$LASTEXITCODE -ne 0" in code
    # Zuweisung vor der ersten Nutzung (Firewall).
    assert code.index("$WebPort = [int]") < code.index('"EXO SMTP Relay - Web $WebPort"')
