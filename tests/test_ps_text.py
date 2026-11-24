"""Werte in PowerShell-Skripttext nur als Literal (v0.2.28, wie Gateway v1.10.117).

Bis v0.2.27 setzten einige Skripte Werte aus den Einstellungen (Relay-Name,
App-ID, Organisation, Zertifikatspfad) roh in einfache Anführungszeichen ein.
Ein Apostroph im Relay-Namen brach das Skript, ein `'` gefolgt von PowerShell-
Code liefe mit den Rechten der Exchange-Anmeldung. Siehe `app/ps_text.py`.
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))

import ps_text  # noqa: E402

# Woran eine Anweisung als PowerShell-Skripttext zu erkennen ist (Gateway-Liste
# plus die Cmdlets, die nur das Relay benutzt).
MERKMALE = ("Connect-ExchangeOnline", "$ErrorActionPreference", "-Identity",
            "Get-ServicePrincipal", "New-ServicePrincipal", "Get-TransportRule",
            "Get-EXOMailbox", "X509Certificate2", "ConvertTo-Json",
            "Get-RoleGroup", "Get-InboundConnector", "Get-Command")

MASKIERER = {"lit", "quoted", "array", "kommentar", "_q"}

# Eingesetzt ohne Maskierer — jeweils mit Grund. Derzeit keine.
ERLAUBT: dict[str, str] = {}


def _skript_stellen():
    for datei in sorted(APP.rglob("*.py")):
        quelle = datei.read_text(encoding="utf-8")
        baum = ast.parse(quelle)
        for anweisung in ast.walk(baum):
            if not isinstance(anweisung, ast.stmt) or isinstance(
                    anweisung, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                ast.If, ast.For, ast.While, ast.Try, ast.With)):
                continue
            text = "".join(k.value for k in ast.walk(anweisung)
                           if isinstance(k, ast.Constant) and isinstance(k.value, str))
            if not any(m in text for m in MERKMALE):
                continue
            for k in ast.walk(anweisung):
                if isinstance(k, ast.FormattedValue):
                    yield datei, k


def test_jeder_eingesetzte_wert_ist_maskiert():
    roh = []
    for datei, k in _skript_stellen():
        w = k.value
        if isinstance(w, ast.Call):
            f = w.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in MASKIERER:
                continue
        if isinstance(w, ast.Name) and w.id in ERLAUBT:
            continue
        roh.append(f"{datei.relative_to(APP)}:{k.lineno} {{{ast.unparse(w)}}}")
    assert not roh, "PowerShell-Text mit unmaskiertem Wert:\n  " + "\n  ".join(roh)


def test_suche_findet_skripte():
    """Gegenprobe: Die Erkennung erfasst die bekannten Skripte überhaupt."""
    dateien = {d.name for d, _ in _skript_stellen()}
    assert {"exo_setup.py", "exo_mailboxes.py", "rechte.py"} <= dateien


def test_rechte_maskiert_ueber_ps_text():
    import rechte
    assert rechte._q("O'Brien\nx") == ps_text.lit("O'Brien\nx")


@pytest.mark.parametrize("wert,erwartet", [
    ("O'Brien GmbH", "O''Brien GmbH"),
    ("a\r\nRemove-Mailbox x", "a  Remove-Mailbox x"),
    ("$(Remove-Item /)", "$(Remove-Item /)"),        # in '…' wirkungslos
    (None, ""),
])
def test_lit(wert, erwartet):
    assert ps_text.lit(wert) == erwartet


def test_array():
    assert ps_text.array(["a", "b'c"]) == "@('a', 'b''c')"


@pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh nicht installiert")
def test_powershell_liest_den_wert_woertlich():
    boese = "Firma $(Write-Output INJIZIERT) O'Brien`n; \"x\""
    r = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                        f"$x = {ps_text.quoted(boese)}; Write-Output $x"],
                       capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == boese


def test_connector_pruefung_setzt_den_namen_als_literal(monkeypatch):
    """Der Relay-Name steht im Skript zur Connector-Prüfung."""
    import exo_setup
    werte = {"GATEWAY_NAME": "O'Brien'; Remove-InboundConnector x; '",
             "CLIENT_ID": "11111111-2222-3333-4444-555555555555",
             "TENANT_DOMAIN": "firma.onmicrosoft.com"}
    monkeypatch.setattr(exo_setup.settings_store, "get", lambda k, *a, **kw: werte.get(k, ""))
    monkeypatch.setattr(exo_setup, "_voraussetzungen", lambda: (True, ""))
    gesehen = {}
    monkeypatch.setattr(exo_setup, "_run_inline",
                        lambda s, **kw: gesehen.update(s=s) or {"ok": True, "output": "CONNECTOR-FEHLT"})
    exo_setup.connector_pruefen()
    assert ("-Identity 'O''Brien''; Remove-InboundConnector x; '' - Inbound'"
            in gesehen["s"]), gesehen["s"]
