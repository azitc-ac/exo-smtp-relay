#!/bin/sh
# Einstieg des Containers: startet als root, macht das Datenverzeichnis für
# appuser beschreibbar und wechselt DANN auf appuser. Der Dienst selbst läuft
# nie als root.
#
# WARUM: docker-compose.yml bindet ./data:/app/data ein. Fehlt ./data auf dem
# Wirt, legt Docker es als root an — appuser (uid 1000) konnte dort nichts
# schreiben, main.py brach mit „PermissionError: '/app/data/logs'“ ab und der
# Container startete in Schleife (Labtest 0.2.42). Ein benanntes Volume hätte
# das vermieden, hätte aber bestehende Installationen mit ./data gebrochen.
#
# Nur was NICHT appuser gehört, wird umgeschrieben (find ! -user): Bei einer
# laufenden Installation ist das nichts, beim ersten Start das leere
# Verzeichnis. Scheitert chown (z. B. NFS mit root_squash), bleibt es bei einer
# Warnung — dann muss der Betreiber den Besitz selbst setzen.
#
# Läuft der Container ohnehin nicht als root (`user:` in compose, rootless),
# startet das Skript den Befehl unverändert.
#
# Für Tests überschreibbar: RELAY_DATEN (Verzeichnis), RELAY_NUTZER.
set -eu

DATEN="${RELAY_DATEN:-${DATA_DIR:-/app/data}}"
NUTZER="${RELAY_NUTZER:-appuser}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATEN"
    if [ -n "$(find "$DATEN" ! -user "$NUTZER" -print -quit)" ]; then
        echo "exo-smtp-relay: setze Besitz von $DATEN auf $NUTZER" >&2
        find "$DATEN" ! -user "$NUTZER" -exec chown -h "$NUTZER:$NUTZER" {} + \
            || echo "exo-smtp-relay: WARNUNG: chown auf $DATEN fehlgeschlagen — Besitz bitte selbst auf uid $(id -u "$NUTZER") setzen" >&2
    fi
    # setpriv lässt die Umgebung stehen; pwsh legt Modul-Cache unter $HOME an.
    HOME="$(getent passwd "$NUTZER" | cut -d: -f6)"
    USER="$NUTZER"
    export HOME USER
    exec setpriv --reuid="$NUTZER" --regid="$NUTZER" --init-groups -- "$@"
fi

exec "$@"
