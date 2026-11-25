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

AKTUALISIERUNG (seit 0.2.40)
----------------------------
Der Wrapper ist auch der Tauscher: Erscheint `data\\update\\auftrag.json`
(geschrieben von der Weboberfläche NACH Prüfung von Signatur und Prüfsumme),
ruft er `app\\update_tausch.ablauf()` auf — erneut prüfen, Prozess anhalten,
tauschen, starten, bei Fehlstart zurückrollen. `waechter.json` meldet der
Oberfläche, dass dieser Wrapper das kann; ein älterer schreibt sie nicht.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
APP = WURZEL / "app"
DATA = WURZEL / "data"
LOGDATEI = DATA / "logs" / "service.log"
VENV = WURZEL / "venv"
UPDATE = DATA / "update"
AUFTRAG = UPDATE / "auftrag.json"
WAECHTER = UPDATE / "waechter.json"
PROTOKOLL = 1                  # muss update_tausch.PROTOKOLL entsprechen
# Fassung DIESES Wrappers, beim Laden festgehalten — nach einem Tausch liegt auf der Platte schon
# die neue, geladen ist bis zum Neustart des Windows-Dienstes aber diese (update_tausch.wrapper_abgleichen).
import hashlib as _hashlib                                     # noqa: E402
WRAPPER_FASSUNG = _hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]

# pythonservice.exe bettet den Basis-Interpreter ein und ignoriert pyvenv.cfg:
# die Pakete der venv (pywin32, servicemanager, ...) liegen sonst nicht im Suchpfad.
for _pfad in (VENV / "Lib" / "site-packages", VENV / "Lib" / "site-packages" / "win32",
              VENV / "Lib" / "site-packages" / "win32" / "lib"):
    if _pfad.is_dir() and str(_pfad) not in sys.path:
        sys.path.append(str(_pfad))

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
        umgebung["RELAY_BETRIEBSART"] = "windows"
        # stdout/stderr des Kindes landen in service.log. Ohne Konsole nimmt Python dafür die
        # ANSI-Codepage (cp1252): „übersprungen“ stand dort als 0xFC (Labtest 0.2.43).
        # Fest UTF-8 wie app.log - gesetzt, nicht setdefault: eine geerbte Codepage zählt nicht.
        umgebung["PYTHONIOENCODING"] = "utf-8"
        # Im Dienst ist sys.executable pythonservice.exe - der Kindprozess nimmt die venv.
        python = VENV / "Scripts" / "python.exe"
        # Das Kind erbt einen eigenen Handle; der des Wrappers wird gleich wieder geschlossen
        # (vorher blieb je Neustart einer offen).
        with open(LOGDATEI, "ab") as ausgabe:
            return subprocess.Popen(
                [str(python if python.exists() else sys.executable), str(APP / "main.py")], cwd=str(APP),
                env=umgebung, stdout=ausgabe, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _waechter_melden(self) -> None:
        try:
            UPDATE.mkdir(parents=True, exist_ok=True)
            WAECHTER.write_text(json.dumps({"protokoll": PROTOKOLL, "art": "windows",
                                            "pid": os.getpid(), "fassung": WRAPPER_FASSUNG}), encoding="utf-8")
        except OSError:
            pass

    def _neu_starten(self) -> None:
        self._proc = self._starten()

    def _laeuft(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    def _aktualisieren(self) -> None:
        """Auftrag ausführen — mit dem update_tausch.py der LAUFENDEN Fassung,
        frisch von der Platte (nicht aus einem früheren Import)."""
        try:
            spec = importlib.util.spec_from_file_location("_update_tausch", APP / "update_tausch.py")
            tausch = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tausch)
            ende = tausch.ablauf(WURZEL, DATA, stoppen=self._beenden,
                                 starten=self._neu_starten, laeuft=self._laeuft)
            servicemanager.LogInfoMsg(f"EXO SMTP Relay: Aktualisierung beendet ({ende})")
        except Exception as exc:                               # noqa: BLE001
            servicemanager.LogErrorMsg(f"EXO SMTP Relay: Aktualisierung abgebrochen: {exc}")
            try:
                AUFTRAG.unlink()
            except OSError:
                pass
        if not self._laeuft():
            self._proc = self._starten()

    def _nachholen(self) -> None:
        """Eine offene Rücknahme (data\\update\\ruecknahme_offen.json, Sperre bei der letzten)
        erneut versuchen, BEVOR die Anwendung startet (Labtest 0.2.53 l4b)."""
        if not (UPDATE / "ruecknahme_offen.json").exists() and not (UPDATE / "wrapper_neustart.json").exists():
            return
        try:
            spec = importlib.util.spec_from_file_location("_update_tausch", APP / "update_tausch.py")
            tausch = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tausch)
            ok = tausch.beim_dienststart(WURZEL, DATA, WRAPPER_FASSUNG)   # derselbe Pfad wie systemd
            if ok is True:
                servicemanager.LogInfoMsg("EXO SMTP Relay: offene Rücknahme nachgeholt")
            elif ok is False:
                servicemanager.LogWarningMsg("EXO SMTP Relay: offene Rücknahme NICHT nachgeholt - siehe Aktualisierung")
        except Exception as exc:                               # noqa: BLE001
            servicemanager.LogErrorMsg(f"EXO SMTP Relay: Rücknahme nicht nachgeholt: {exc}")

    def _lauf(self) -> None:
        fehlstarts = 0
        self._waechter_melden()
        self._nachholen()
        while True:
            self._proc = self._starten()
            gestartet = time.monotonic()
            while True:
                if win32event.WaitForSingleObject(self._stop, 1000) == win32event.WAIT_OBJECT_0:
                    self._beenden()
                    return
                if AUFTRAG.exists():
                    self._aktualisieren()
                    gestartet = time.monotonic()
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
