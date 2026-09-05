"""Login-Drosselung: exponentielles Backoff nach Fehlversuchen.

Gegen Brute-Force auf `/auth/local` und den HTTP-Basic-Notzugang. PBKDF2 bremst
einen einzelnen Versuch, sperrt aber nicht — deshalb hier eine Zählung pro
Schlüssel (IP bzw. Benutzer).

In-Memory und pro Prozess: Der Listener ist einer, und ein Neustart, der die
Zähler leert, ist bei Brute-Force unkritisch (der Angreifer muss von vorn
beginnen). Kein Persistenzbedarf, keine DB.
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_FEHLER: dict[str, list[float]] = {}   # Schlüssel → Zeitstempel der Fehlversuche

_FENSTER = 900.0        # 15 min: nur Fehlversuche in diesem Fenster zählen
_FREI = 3               # die ersten 3 Fehlversuche kosten nichts (Vertipper)
_DECKEL = 300.0         # Sperre nie länger als 5 min


def _jetzt() -> float:
    return time.time()


def _aktuelle(schluessel: str) -> list[float]:
    ts = [t for t in _FEHLER.get(schluessel, []) if _jetzt() - t < _FENSTER]
    if ts:
        _FEHLER[schluessel] = ts
    else:
        _FEHLER.pop(schluessel, None)
    return ts


def sperr_sekunden(schluessel: str) -> float:
    """Verbleibende Sperrzeit in Sekunden (0.0 = frei).

    Ab dem (`_FREI`+1)-ten Fehlversuch greift ein exponentielles Backoff
    (2, 4, 8, … s, gedeckelt), gemessen ab dem letzten Fehlversuch.
    """
    with _LOCK:
        ts = _aktuelle(schluessel)
        n = len(ts)
        if n <= _FREI:
            return 0.0
        dauer = min(_DECKEL, 2.0 ** (n - _FREI))
        rest = dauer - (_jetzt() - ts[-1])
        return max(0.0, rest)


def fehlversuch(schluessel: str) -> None:
    with _LOCK:
        _FEHLER.setdefault(schluessel, []).append(_jetzt())


def erfolg(schluessel: str) -> None:
    """Nach erfolgreicher Anmeldung die Zählung für den Schlüssel löschen."""
    with _LOCK:
        _FEHLER.pop(schluessel, None)


def gesperrt(schluessel: str) -> bool:
    return sperr_sekunden(schluessel) > 0.0
