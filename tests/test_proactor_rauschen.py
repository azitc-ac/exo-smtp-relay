"""Windows-Proactor: ConnectionResetError beim Aufraeumen einer Verbindung soll das Protokoll nicht fuellen."""
import logging
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))


def _satz(msg, exc):
    return logging.LogRecord("asyncio", logging.ERROR, "x", 1, msg, None, (type(exc), exc, None))


def test_verbindungsabbruch_beim_aufraeumen_wird_gefiltert():
    import main
    f = main._ProactorRauschen()
    assert f.filter(_satz("Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)", ConnectionResetError())) is False


def test_andere_asyncio_fehler_bleiben_sichtbar():
    import main
    f = main._ProactorRauschen()
    assert f.filter(_satz("Task exception was never retrieved", RuntimeError("x"))) is True
    assert f.filter(_satz("Exception in callback foo()", ConnectionResetError())) is True
