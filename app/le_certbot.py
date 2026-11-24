"""Let's Encrypt via certbot (HTTP-01, webroot) — Ausstellen UND Erneuern aus EINER Quelle.

Übernommen aus dem Gateway (le_certbot.py). Dort der Grund für dieses Modul:
Ausstellung und Erneuerung waren getrennte Code-Wege und drifteten auseinander —
der Renew nahm certbots Default-Verzeichnisse (root-eigen), fand die
Renewal-Konfiguration nie und kopierte das Zertifikat auch nicht an den
Listener-Pfad. Hier nutzen `ausstellen()` und `erneuern()` dieselben
Verzeichnisse (`_dir_flags`) und dieselbe Übernahme (`uebernehmen`).

ABWEICHUNGEN VOM GATEWAY (alle begründet):

* **Port 80 nur während des certbot-Laufs.** Das Gateway betreibt dauerhaft
  einen Port-80-Dienst (Einrichtungsseite, Umleitung auf HTTPS). Das Relay hat
  keinen; `_challenge_dienst()` öffnet Port 80 nur für die Dauer von
  `ausstellen()`/`erneuern()` und liefert ausschließlich Dateien unter
  `/.well-known/acme-challenge/` aus dem Webroot — mit derselben
  Containment-Prüfung wie das Gateway (`_acme_challenge_datei`).
* **RSA-Schlüssel** (`--key-type rsa`). certbot nimmt seit 2.0 ECDSA. Alte
  Geräte, für die es das Relay gibt, können oft nur statischen RSA-
  Schlüsseltausch (siehe `main._build_tls_context`) — mit einem EC-Zertifikat
  bekämen sie keinen Handshake.
* **Übernahme über `tls_cert._schreiben`** statt `shutil.copy2`: derselbe Weg
  wie PFX-Import und selbstsigniertes Zertifikat, mit `secure_io` für die
  Rechte. Der Listener lädt das Zertifikat danach von selbst
  (`main._LenientController.tls_auffrischen`), der Rückweg zu Exchange bei
  jedem Versand — kein Neustart.
* **Plattform:** certbot gibt es für Windows nicht mehr (eingestellt mit 2.11,
  2024). `verfuegbar()` sagt, ob certbot da ist; unter Windows bleiben
  DNS-01 (`tls_acme_dns.py`) und der PFX-Import. Im Docker-Abbild ist certbot
  enthalten, unter Linux ohne Docker `apt install certbot`.
* **Proxy:** `ACME_HTTP_PROXY` gilt wie für `acme_client` auch für certbot
  (als HTTPS_PROXY im Umfeld des Unterprozesses).

KEIN root nötig — alle Pfade liegen unter `DATA_DIR`. Port 80 braucht das Recht
zum Binden niedriger Ports, das der Dienst für Port 25 ohnehin hat
(Docker: NET_BIND_SERVICE, systemd: AmbientCapabilities).
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
# certbot mit fester Argumentliste, ohne Shell:
import subprocess  # nosec B404
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
import settings_store

log = logging.getLogger(__name__)

CERT_NAME = "relay"
HTTP01_PORT = 80
_TIMEOUT_ISSUE = 120
# Großzügig: der eigentliche Fallstrick im Gateway war nicht die Dauer, sondern
# certbots Zufallsverzögerung — siehe --no-random-sleep-on-renew in erneuern().
_TIMEOUT_RENEW = 300
_SCHLUESSEL = ["--key-type", "rsa", "--rsa-key-size", "2048"]


def _dirs() -> dict[str, Path]:
    d = Path(config.DATA_DIR)
    return {
        "webroot": d / "acme-webroot",
        "config": d / "le-config",
        "work": d / "le-work",
        "logs": d / "le-logs",
    }


def _dir_flags() -> list[str]:
    """Die drei Verzeichnis-Flags — GEMEINSAM für Ausstellen und Erneuern."""
    dd = _dirs()
    return ["--config-dir", str(dd["config"]),
            "--work-dir", str(dd["work"]),
            "--logs-dir", str(dd["logs"])]


def _live() -> Path:
    return _dirs()["config"] / "live" / CERT_NAME


def verfuegbar() -> bool:
    """Ist certbot installiert? (Windows: nein — siehe Modulkopf.)"""
    return shutil.which("certbot") is not None


def verwaltet() -> bool:
    """True, wenn eine certbot-Renewal-Konfiguration für uns existiert UND der
    Listener gerade certbots Zertifikat ausliefert.

    ⚠️ Die zweite Bedingung fehlt im Gateway: Wer nach einer Ausstellung über
    certbot ein PFX importiert oder DNS-01 nimmt, behält die Renewal-
    Konfiguration. Ohne den Abgleich überschriebe die nächste Erneuerung das
    bewusst gesetzte Zertifikat mit dem von certbot."""
    if not (_dirs()["config"] / "renewal" / f"{CERT_NAME}.conf").exists():
        return False
    live = _leaf_seriennummer(_live() / "cert.pem")
    return live is not None and live == _leaf_seriennummer(Path(config.SMTP_TLS_CERT))


def _leaf_seriennummer(pfad: Path) -> int | None:
    try:
        from cryptography import x509
        return x509.load_pem_x509_certificate(pfad.read_bytes()).serial_number
    except Exception:                                 # noqa: BLE001
        return None


def _expiry(cert_path: Path) -> datetime | None:
    if not cert_path.exists():
        return None
    try:
        from cryptography import x509
        return x509.load_pem_x509_certificate(cert_path.read_bytes()).not_valid_after_utc
    except Exception as exc:                          # noqa: BLE001
        log.warning("le_certbot: Zertifikat nicht lesbar (%s): %s", cert_path, exc)
        return None


def lauf_vermerken(ok: bool, text: str, weg: str) -> None:
    """Letzten Let's-Encrypt-Vorgang festhalten (Oberfläche, Zeitplaner).

    `weg` (http01/dns01) sagt dem Zeitplaner, ob er nach einer Wiederherstellung
    ohne certbot-Konfiguration neu ausstellen darf (siehe scheduler)."""
    settings_store.update({"LE_LAUF": {
        "zeit": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ok": bool(ok), "text": (text or "")[:500], "weg": weg}})


def uebernehmen() -> str:
    """certbot-Live-Zertifikat an den Listener-Pfad (SMTP_TLS_CERT/KEY) schreiben.
    Gibt das Ablaufdatum (dd.mm.YYYY) zurück. Wirft OSError.

    ⚠️ Der Listener liest SMTP_TLS_CERT, nicht das certbot-Live-Verzeichnis.
    Ohne diese Übernahme bliebe ein erneuertes Zertifikat unbenutzt."""
    import tls_cert
    live = _live()
    tls_cert._schreiben((live / "fullchain.pem").read_bytes(),
                        (live / "privkey.pem").read_bytes())
    exp = _expiry(Path(config.SMTP_TLS_CERT))
    return exp.strftime("%d.%m.%Y") if exp else "?"


# ── Port 80 nur für die Dauer des certbot-Laufs ──────────────────────────────

def _acme_challenge_datei(webroot: Path, urlpfad: str) -> Path | None:
    """Sichere Auflösung einer `/.well-known/acme-challenge/`-Anfrage (aus dem
    Gateway). `BaseHTTPRequestHandler` normalisiert `..` NICHT — ohne die
    Containment-Prüfung ließe sich über Port 80 settings.json auslesen."""
    try:
        pfad = urlpfad.split("?", 1)[0]
        if not pfad.startswith("/.well-known/acme-challenge/"):
            return None
        ziel = (webroot / pfad.lstrip("/")).resolve()
        if ziel.is_relative_to(webroot.resolve()) and ziel.is_file():
            return ziel
    except (OSError, ValueError):
        pass
    return None


@contextlib.contextmanager
def _challenge_dienst():
    """Port 80 öffnen, nur Challenge-Dateien ausliefern, danach schließen.
    Wirft OSError, wenn der Port nicht frei ist."""
    webroot = _dirs()["webroot"]
    webroot.mkdir(parents=True, exist_ok=True)

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            datei = _acme_challenge_datei(webroot, self.path)
            if datei is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(datei.read_bytes())

        def log_message(self, *args):
            pass

    # Alle Adressen: Let's Encrypt prüft von außen.
    server = ThreadingHTTPServer(("0.0.0.0", HTTP01_PORT), _Handler)  # nosec B104
    faden = threading.Thread(target=server.serve_forever, name="acme-http01", daemon=True)
    faden.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


def _umgebung() -> dict:
    env = dict(os.environ)
    proxy = (settings_store.get("ACME_HTTP_PROXY") or "").strip()
    if proxy:
        env["HTTPS_PROXY"] = env["https_proxy"] = proxy
    return env


def _port_fehler(exc: OSError) -> str:
    return (f"Port {HTTP01_PORT} lässt sich nicht öffnen ({exc}). Let's Encrypt prüft "
            f"HTTP-01 über Port {HTTP01_PORT} — ist er belegt, nimm DNS-01.")


def ausstellen(hostname: str, email: str) -> tuple[bool, str]:
    """`certbot certonly --webroot`. Rückgabe (True, Ablaufdatum) bei Erfolg,
    sonst (False, Fehlertext)."""
    if not verfuegbar():
        return False, "certbot ist nicht installiert (unter Windows nicht verfügbar) — nimm DNS-01."
    dd = _dirs()
    for d in dd.values():
        d.mkdir(parents=True, exist_ok=True)
    cmd = (["certbot", "certonly", "--webroot", "-w", str(dd["webroot"]),
            "-d", hostname, "--cert-name", CERT_NAME, *_SCHLUESSEL,
            "--email", email, "--agree-tos", "--non-interactive"] + _dir_flags())
    try:
        with _challenge_dienst():
            res = subprocess.run(cmd, capture_output=True, text=True,  # nosec B603
                                 timeout=_TIMEOUT_ISSUE, env=_umgebung())
    except OSError as exc:
        return False, _port_fehler(exc)
    except subprocess.TimeoutExpired:
        return False, f"certbot antwortete nicht binnen {_TIMEOUT_ISSUE} s"
    if res.returncode != 0:
        return False, (res.stderr or res.stdout or "certbot error").strip()
    try:
        return True, uebernehmen()
    except OSError as exc:
        return False, f"certbot OK, aber Übernahme fehlgeschlagen: {exc}"


def erneuern(force: bool = False) -> tuple[str, str]:
    """`certbot renew` mit DENSELBEN Verzeichnissen wie `ausstellen()`, danach
    Übernahme ans Listener-Zertifikat.

    Rückgabe (status, info):
      ``renewed``     info = neues Ablaufdatum (Live-Zert war neuer → übernommen)
      ``unchanged``   info = ''  (certbot lief, aber nichts erneuert)
      ``error``       info = Fehlertext
      ``not_managed`` info = ''  (keine Renewal-Konfig — z. B. DNS-01-Zert)

    Übernimmt NUR bei tatsächlicher Erneuerung. Einen Neustart braucht es nicht:
    Der Listener lädt das geänderte Zertifikat bei der nächsten Verbindung."""
    if not verwaltet():
        return "not_managed", ""
    if not verfuegbar():
        return "error", "certbot nicht gefunden"
    served_before = _expiry(Path(config.SMTP_TLS_CERT))
    # ⚠️ --no-random-sleep-on-renew ist ZWINGEND (Gateway, live gemessen):
    # certbot legt sonst bis zu ~8 Minuten Zufallsverzögerung vor die
    # Erneuerung, die das Timeout auffrisst.
    cmd = (["certbot", "renew", "--cert-name", CERT_NAME,
            "--non-interactive", "--no-random-sleep-on-renew", "--quiet"] + _dir_flags())
    if force:
        cmd.append("--force-renewal")
    try:
        with _challenge_dienst():
            res = subprocess.run(cmd, capture_output=True, text=True,  # nosec B603
                                 timeout=_TIMEOUT_RENEW, env=_umgebung())
    except OSError as exc:
        return "error", _port_fehler(exc)
    except Exception as exc:                          # noqa: BLE001
        return "error", str(exc)
    if res.returncode != 0:
        return "error", (res.stderr or res.stdout or f"rc={res.returncode}").strip()
    live_exp = _expiry(_live() / "fullchain.pem")
    if live_exp and (served_before is None or live_exp > served_before):
        try:
            return "renewed", uebernehmen()
        except OSError as exc:
            return "error", f"erneuert, aber Übernahme fehlgeschlagen: {exc}"
    return "unchanged", ""
