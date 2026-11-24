"""Docker: ein root-eigenes ./data darf den Start nicht verhindern (0.2.43).

Labtest 0.2.42: docker-compose.yml bindet ./data ein; fehlt es, legt Docker es
als root an. Das Abbild lief als appuser → PermissionError auf
/app/data/logs, Neustartschleife. Jetzt startet der Container als root,
docker-entrypoint.sh setzt den Besitz und wechselt auf appuser.

Der Ablauf wird hier mit Attrappen für id/chown/setpriv/getent geprüft (kein
root nötig); den echten Start mit frischem Bind-Mount prüft die CI
(Job „Container baut“).
"""
import os
import re
import subprocess
import sys

import pytest

from conftest import WURZEL

SKRIPT = WURZEL / "docker-entrypoint.sh"
DOCKERFILE = (WURZEL / "Dockerfile").read_text(encoding="utf-8")

posix = pytest.mark.skipif(sys.platform == "win32", reason="Shell-Skript des Containers")


def test_dockerfile_startet_ueber_den_einstieg_nicht_als_appuser():
    # Mit `USER appuser` fehlte dem Einstieg das Recht zum chown.
    assert not re.search(r"^\s*USER\s+appuser", DOCKERFILE, re.M)
    assert 'ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]' in DOCKERFILE
    assert "COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh" in DOCKERFILE
    assert "useradd -m -u 1000 appuser" in DOCKERFILE


def test_compose_bleibt_beim_bind_mount():
    # Bestehende Installationen haben ihre Daten in ./data — kein Wechsel auf ein Volume.
    assert "- ./data:/app/data" in (WURZEL / "docker-compose.yml").read_text(encoding="utf-8")


def test_skript_hat_lf_zeilenenden():
    assert b"\r\n" not in SKRIPT.read_bytes()
    assert "*.sh text eol=lf" in (WURZEL / ".gitattributes").read_text(encoding="utf-8")


def _attrappen(tmp_path, uid: str, protokoll):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    eigen = str(os.getuid())
    stuecke = {
        # id -u → gespielte uid; id -u appuser → echte uid (gehört „appuser“)
        "id": f'if [ "$#" -eq 1 ]; then echo {uid}; else echo {eigen}; fi',
        "chown": f'echo "chown $*" >> {protokoll}',
        "setpriv": f'echo "setpriv $* HOME=$HOME" >> {protokoll}',
        "getent": 'echo "appuser:x:1000:1000::/home/appuser:/bin/sh"',
        # find ! -user appuser: „appuser“ ist hier der Testnutzer selbst —
        # Dateien eines ANDEREN Besitzers simuliert die Markierung .root-eigen.
        "find": f'''d="$1"; shift
if [ -e "$d/.root-eigen" ]; then
  case "$*" in *-quit*) echo "$d";; *-exec*) echo "chown -h appuser:appuser $d" >> {protokoll};; esac
fi''',
    }
    for name, text in stuecke.items():
        p = bin_ / name
        p.write_text("#!/bin/sh\n" + text + "\n")
        p.chmod(0o755)
    return bin_


def _lauf(tmp_path, daten, uid="0"):
    protokoll = tmp_path / "protokoll.txt"
    protokoll.touch()
    bin_ = _attrappen(tmp_path, uid, protokoll)
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}",
               RELAY_DATEN=str(daten), RELAY_NUTZER="appuser", HOME="/root")
    r = subprocess.run(["sh", str(SKRIPT), "echo", "gestartet"], env=env,
                       capture_output=True, text=True, timeout=20)
    return r, protokoll.read_text()


@posix
def test_root_eigenes_datenverzeichnis_wird_appuser_uebergeben(tmp_path):
    daten = tmp_path / "data"
    daten.mkdir()
    (daten / ".root-eigen").touch()          # wie von Docker als root angelegt
    r, prot = _lauf(tmp_path, daten)
    assert r.returncode == 0, r.stderr
    assert f"chown -h appuser:appuser {daten}" in prot
    assert "setpriv --reuid=appuser --regid=appuser --init-groups -- echo gestartet" in prot
    assert "HOME=/home/appuser" in prot


@posix
def test_fehlendes_datenverzeichnis_wird_angelegt(tmp_path):
    daten = tmp_path / "gibt-es-nicht"
    r, prot = _lauf(tmp_path, daten)
    assert r.returncode == 0, r.stderr
    assert daten.is_dir()
    assert "setpriv" in prot


@posix
def test_passender_besitz_bleibt_unangetastet(tmp_path):
    daten = tmp_path / "data"
    daten.mkdir()
    r, prot = _lauf(tmp_path, daten)
    assert r.returncode == 0, r.stderr
    assert "chown" not in prot
    assert "setpriv" in prot


@posix
def test_ohne_root_startet_der_befehl_unveraendert(tmp_path):
    daten = tmp_path / "data"
    daten.mkdir()
    (daten / ".root-eigen").touch()
    r, prot = _lauf(tmp_path, daten, uid="1000")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "gestartet"
    assert prot == ""
