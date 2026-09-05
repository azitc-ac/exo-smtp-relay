"""Trace-ID pro Mail — alle Log-Zeilen einer SMTP-Transaktion bekommen
denselben "[mail:xxxxxxxx]"-Prefix (analog zu den ACME-Flow-IDs).

Suche nach der ID in der Protokoll-Suche liefert das komplette Bild einer
Nachricht über alle Module hinweg (Handler, Signatur, S/MIME, Reinject …),
ohne dass die einzelnen Log-Aufrufe angefasst werden müssen: Ein
logging.Filter auf den Root-Handlern injiziert den Prefix, solange die
contextvar gesetzt ist. contextvars sind asyncio-Task-lokal — parallele
SMTP-Transaktionen bekommen dadurch getrennte IDs.
"""
import contextvars
import logging
import uuid

_current: contextvars.ContextVar[str] = contextvars.ContextVar("mail_trace_id", default="")


def new_trace() -> str:
    """Neue Trace-ID für die aktuelle Transaktion setzen (Aufruf in handle_DATA)."""
    tid = uuid.uuid4().hex[:8]
    _current.set(tid)
    return tid


def get() -> str:
    return _current.get()


class TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        tid = _current.get()
        if tid:
            msg = record.msg if isinstance(record.msg, str) else str(record.msg)
            if not msg.startswith("[mail:") and not msg.startswith("[acme:"):
                record.msg = f"[mail:{tid}] {msg}"
        return True


def install() -> None:
    """Filter auf alle Root-Handler legen — nach log_manager.setup() aufrufen."""
    f = TraceFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(f)
