"""Protokolldateien: Tagesrotation, Aufräumen, Suche.

Übernommen aus dem Gateway. ⚠️ `zoneinfo` braucht unter Windows das Paket
`tzdata` (Windows hat keine IANA-Zonendatenbank) — es steht in
requirements.txt, sonst fiele jede Zeitzone still auf UTC zurück.
"""
import logging
import logging.handlers
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import zoneinfo
import config

LOG_DIR = Path(config.DATA_DIR) / "logs"
_LOG_FILENAME = "app.log"
_ROTATED_RE = re.compile(r"app\.log\.\d{4}-\d{2}-\d{2}$")

_LOG_FMT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


class _TZFormatter(logging.Formatter):
    """Formatter that renders %(asctime)s in a configurable IANA timezone."""

    def __init__(self, tz_name: str = "UTC", *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self._tz = zoneinfo.ZoneInfo(tz_name)
        except Exception:
            self._tz = timezone.utc

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=self._tz)
        return dt.strftime(datefmt or _DATE_FMT)


def setup(retention_days: int = 30, tz_name: str = "UTC") -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = _TZFormatter(tz_name, _LOG_FMT, datefmt=_DATE_FMT)

    # Apply timezone formatter to all existing root handlers (e.g. StreamHandler
    # added by logging.basicConfig before this function runs).
    root = logging.getLogger()
    for h in root.handlers:
        h.setFormatter(formatter)

    handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(LOG_DIR / _LOG_FILENAME),
        when="midnight",
        backupCount=max(1, retention_days),
        encoding="utf-8",
        utc=False,
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
    _cleanup(retention_days)


def _cleanup(retention_days: int) -> None:
    if not LOG_DIR.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    for f in LOG_DIR.iterdir():
        if f.name == _LOG_FILENAME or _ROTATED_RE.match(f.name):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    f.unlink()
                    logging.getLogger(__name__).info("Deleted old log file: %s", f.name)
            except Exception:
                pass


_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}")


def _norm_ts(v: str) -> str:
    """'2026-07-11T14:30' (datetime-local) → '2026-07-11 14:30' — lexikalisch
    vergleichbar mit dem Zeilen-Prefix des Log-Formats (%Y-%m-%d %H:%M:%S)."""
    return (v or "").strip().replace("T", " ")[:16]


def search(query: str, max_lines: int = 500,
           time_from: str = "", time_to: str = "") -> list[str]:
    """Log-Zeilen suchen (case-insensitive), optional auf einen Zeitraum
    eingegrenzt. Bei gesetztem Zeitraum darf query leer sein (alles im
    Zeitfenster). Zeilen ohne Zeitstempel (z.B. Traceback-Folgezeilen)
    passieren den Zeitfilter."""
    time_from, time_to = _norm_ts(time_from), _norm_ts(time_to)
    if not query and not (time_from or time_to):
        return []
    if not LOG_DIR.exists():
        return []
    pattern = re.compile(re.escape(query), re.IGNORECASE) if query else None

    _has_time_filter = bool(time_from or time_to)

    def _ts_in_range(ts: str) -> bool:
        if time_from and ts < time_from:
            return False
        if time_to and ts > time_to:
            return False
        return True

    results: list[str] = []

    # Collect all matching files: rotated (newest first) + current
    files = sorted(
        [f for f in LOG_DIR.iterdir() if _ROTATED_RE.match(f.name)],
        reverse=True,
    )
    current = LOG_DIR / _LOG_FILENAME
    if current.exists():
        files.insert(0, current)

    for f in files:
        try:
            matches: list[str] = []
            with f.open(encoding="utf-8", errors="replace") as fh:
                # Zeilen ohne Zeitstempel (Tracebacks, Uvicorn-Banner) erben
                # die Zeitraum-Entscheidung der letzten gestempelten Zeile
                in_range = not _has_time_filter
                for ln in fh:
                    if _has_time_filter and _TS_RE.match(ln):
                        in_range = _ts_in_range(ln[:16])
                    if in_range and (pattern is None or pattern.search(ln)):
                        matches.append(ln.rstrip("\n"))
            # From each file, take matches in reverse (newest lines first)
            results.extend(reversed(matches))
            if len(results) >= max_lines:
                break
        except Exception:
            pass

    return results[:max_lines]


def list_files() -> list[dict]:
    if not LOG_DIR.exists():
        return []
    result = []
    for f in sorted(LOG_DIR.iterdir(), reverse=True):
        if f.name == _LOG_FILENAME or _ROTATED_RE.match(f.name):
            stat = f.stat()
            result.append({
                "name": f.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "date": datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y"),
            })
    return result
