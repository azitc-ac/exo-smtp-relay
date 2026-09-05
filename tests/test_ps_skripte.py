"""PowerShell-Skripte: mit BOM gespeichert und unter Windows PowerShell 5.1 lauffähig.

Ohne BOM liest PowerShell 5.1 eine UTF-8-Datei als Windows-1252 — Umlaute
werden zu Zeichensalat, und in einem String-Literal bricht das Skript. PS-7-
Syntax (`&&`, `||`, `?:`, `??`) ist in 5.1 ein Parse-Fehler.
"""
import re
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
SKRIPTE = sorted(list((WURZEL / "app" / "scripts").glob("*.ps1")) + list((WURZEL / "windows").glob("*.ps1")))

# PS-7-Konstrukte, die 5.1 nicht kennt. Zeilen mit Kommentar werden nicht geprüft.
PS7 = [
    (re.compile(r"\S\s*&&\s*\S"), "Pipeline-Verkettung `&&`"),
    (re.compile(r"\S\s*\|\|\s*\S"), "Pipeline-Verkettung `||`"),
    (re.compile(r"\?\?"), "Null-Coalescing `??`"),
    (re.compile(r"\?\."), "Null-conditional `?.`"),
    (re.compile(r"\$\w+\s*\?\s*[^:]+:\s*"), "Ternary `? :`"),
    (re.compile(r"::new\("), "`::new()` — in 5.1 nur mit .NET 4.6+; New-Object ist überall sicher"),
    (re.compile(r"\bForEach-Object\s+-Parallel\b"), "ForEach-Object -Parallel"),
]


def _code_zeilen(text: str):
    im_block = False
    for nr, zeile in enumerate(text.splitlines(), 1):
        s = zeile.strip()
        if s.startswith("<#"):
            im_block = True
        if im_block:
            if "#>" in s:
                im_block = False
            continue
        if s.startswith("#"):
            continue
        yield nr, zeile.split(" #")[0]


def test_es_gibt_skripte():
    assert SKRIPTE


def test_bom():
    for s in SKRIPTE:
        assert s.read_bytes()[:3] == b"\xef\xbb\xbf", f"{s.name}: kein UTF-8-BOM"


def test_kein_ps7_only():
    for s in SKRIPTE:
        for nr, zeile in _code_zeilen(s.read_text(encoding="utf-8-sig")):
            for muster, name in PS7:
                assert not muster.search(zeile), f"{s.name}:{nr}: {name}"


def test_keine_umlaute_in_skripten():
    """Belt and braces: Auch mit BOM sind Umlaute in Konsolenausgaben unter 5.1
    je nach Codepage falsch dargestellt — die Skripte kommen ohne aus."""
    for s in SKRIPTE:
        text = s.read_text(encoding="utf-8-sig")
        assert not re.search(r"[äöüÄÖÜß]", text), f"{s.name}: Umlaut gefunden"
