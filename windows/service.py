"""Windows-Dienst — startet `main.py` als Kindprozess und beendet ihn sauber.

WARUM EIN KINDPROZESS UND NICHT `main.main()` IM DIENST
--------------------------------------------------------
Ein Windows-Dienst läuft in einer Umgebung ohne Konsole, mit eigener
Signalbehandlung und einem Arbeitsverzeichnis, das nicht das der Anwendung
ist. `main.py` soll aber ÜBERALL derselbe Aufruf sein (Container, systemd,
Windows). Der Wrapper hält deshalb nur den Prozess: Start beim Dienststart,
`terminate()` beim Stopp, Neustart bei einem Absturz. Alles Fachliche bleibt in
`main.py`, und das läuft unverändert auch im Vordergrund zum Ausprobieren.

Installieren (als Administrator; `install.ps1` tut genau das):
    venv\\Scripts\\python.exe windows\\service.py install
    venv\\Scripts\\python.exe windows\\service.py start
Entfernen:
    venv\\Scripts\\python.exe windows\\service.py stop
    venv\\Scripts\\python.exe windows\\service.py remove
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
APP = WURZEL / "app"
DATA = WURZEL / "data"
LOGDATEI = DATA / "logs" / "service.log"

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except ImportError:                                            # pragma: no cover
    if __name__ == "__main__":
        print("pywin32 fehlt — `pip install pywin32` in der venv der Anwendung.")
        sys.exit(1)
    raise


class RelayService(win32serviceutil.ServiceFramework):
    _svc_name_ = "ExoSmtpRelay"
    _svc_display_name_ = "EXO SMTP Relay"
    _svc_description_ = ("SMTP-Relay für Drucker, Scanner und Anwendungen im eigenen "
                         "Netz — übergibt Post an Exchange Online.")

    def __init__(self, args):
        super().__init__(args)
        self._stop = win32event.CreateEvent(None, 0, 0, None)
        self._proc: subprocess.Popen | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self._stop)

    def SvcDoRun(self):
        servicemanager.LogInfoMsg("EXO SMTP Relay: Dienst startet")
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)
        self._lauf()
        servicemanager.LogInfoMsg("EXO SMTP Relay: Dienst beendet")

    def _starten(self) -> subprocess.Popen:
        (DATA / "logs").mkdir(parents=True, exist_ok=True)
        umgebung = dict(os.environ)
        umgebung.setdefault("DATA_DIR", str(DATA))
        umgebung.setdefault("PYTHONUNBUFFERED", "1")
        # Dieselbe Python-Fassung wie der Dienst selbst (die venv).
        ausgabe = open(LOGDATEI, "ab")
        return subprocess.Popen(
            [sys.executable, str(APP / "main.py")], cwd=str(APP), env=umgebung,
            stdout=ausgabe, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _lauf(self) -> None:
        fehlstarts = 0
        while True:
            self._proc = self._starten()
            gestartet = time.monotonic()
            while True:
                if win32event.WaitForSingleObject(self._stop, 1000) == win32event.WAIT_OBJECT_0:
                    self._beenden()
                    return
                if self._proc.poll() is not None:
                    break
            # Abgestürzt — neu starten, mit wachsender Pause bei Serienfehlern.
            fehlstarts = fehlstarts + 1 if time.monotonic() - gestartet < 60 else 0
            pause = min(60, 5 * (2 ** fehlstarts))
            servicemanager.LogWarningMsg(
                f"EXO SMTP Relay: Prozess endete mit {self._proc.returncode}, Neustart in {pause} s")
            if win32event.WaitForSingleObject(self._stop, pause * 1000) == win32event.WAIT_OBJECT_0:
                return

    def _beenden(self) -> None:
        if not self._proc or self._proc.poll() is not None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._proc.kill()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(RelayService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(RelayService)
