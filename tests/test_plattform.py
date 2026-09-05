"""Der Dienst muss unter Linux UND Windows laufen — kein POSIX-Only im Anwendungscode."""
import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

VERBOTEN = [
    (re.compile(r"^\s*import fcntl|^\s*import pwd\b|^\s*import grp\b|^\s*import resource\b", re.M), "POSIX-only Modul"),
    (re.compile(r"os\.fork\(|os\.geteuid\(|os\.getuid\(|os\.setsid\("), "POSIX-only Aufruf"),
    (re.compile(r"[\"']/app/data[\"']|[\"']/app/certs[\"']"), "Container-Pfad als Literal — config.DATA_DIR nutzen"),
    (re.compile(r"[\"']/proc/"), "/proc gibt es unter Windows nicht"),
    (re.compile(r"subprocess\.run\(\s*\[\s*[\"']openssl[\"']"), "openssl-Binary — cryptography nutzen"),
    (re.compile(r"[\"']pwsh[\"']\s*,"), "Shell fest verdrahtet — config.PWSH nutzen"),
]


def _docstring_zeilen(text: str) -> set[int]:
    """Zeilen, die zu einem Docstring gehören — eine Erwähnung dort ist keine Verwendung."""
    import ast
    aus: set[int] = set()
    try:
        baum = ast.parse(text)
    except SyntaxError:
        return aus
    for knoten in ast.walk(baum):
        if isinstance(knoten, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if knoten.body and isinstance(knoten.body[0], ast.Expr) \
                    and isinstance(getattr(knoten.body[0], "value", None), ast.Constant) \
                    and isinstance(knoten.body[0].value.value, str):
                d = knoten.body[0]
                aus.update(range(d.lineno, (d.end_lineno or d.lineno) + 1))
    return aus


def test_kein_posix_only():
    for datei in APP.rglob("*.py"):
        text = datei.read_text(encoding="utf-8")
        doc = _docstring_zeilen(text)
        zeilen = text.splitlines()
        for muster, name in VERBOTEN:
            for m in muster.finditer(text):
                zeile = text[:m.start()].count("\n") + 1
                if zeile in doc or zeilen[zeile - 1].strip().startswith("#"):
                    continue
                assert False, f"{datei.relative_to(APP)}:{zeile}: {name}"


def test_datenverzeichnis_kommt_aus_config():
    import sys
    sys.path.insert(0, str(APP))
    import config
    assert Path(config.DATA_DIR).is_absolute()
    assert Path(config.SMTP_TLS_CERT).parent == Path(config.DATA_DIR) / "certs"


def test_pwsh_erkennung_faellt_unter_windows_auf_powershell_zurueck(monkeypatch):
    import sys
    sys.path.insert(0, str(APP))
    import config
    monkeypatch.delenv("PWSH", raising=False)
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setattr(config.shutil, "which", lambda n: r"C:\Windows\powershell.exe" if n == "powershell" else None)
    assert config._powershell() == "powershell"
    monkeypatch.setattr(config.shutil, "which", lambda n: "/usr/bin/pwsh" if n == "pwsh" else None)
    assert config._powershell() == "pwsh"
    monkeypatch.setenv("PWSH", r"D:\pwsh\pwsh.exe")
    assert config._powershell() == r"D:\pwsh\pwsh.exe"
