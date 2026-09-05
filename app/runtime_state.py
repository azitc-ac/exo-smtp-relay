"""Prozessweiter Laufzeitzustand, den mehrere Module lesen müssen.

⚠️ Bewusst ein eigenes Modul und NICHT `main`: Der Prozess läuft `main.py` als
`__main__`; ein `import main` aus einem anderen Modul erzeugt ein ZWEITES
Modulobjekt mit eigenen Globals. Ein dort gesetztes Attribut wäre also nicht
dasselbe. Ein neutrales Modul, das alle gleich importieren, teilt den Zustand.
"""
from __future__ import annotations

# Der laufende aiosmtpd-Controller — gesetzt von main._run_smtp nach dem Start,
# gelesen von health_check für die /health-Liveness. None, solange der Listener
# nicht läuft (z. B. in Tests ohne SMTP).
smtp_controller = None
