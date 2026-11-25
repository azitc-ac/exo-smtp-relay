# Ein Release testen

Checkliste für ein neues Release — vor allem vor dem Wechsel von `beta` nach
`stable`. Geprüft wird auf drei Installationen: Windows-Dienst, systemd und
Docker. Nimm **frische** Systeme: Mehrere Fehler zeigten sich nur auf einer
Maschine, auf der noch nichts installiert war.

Platzhalter: `<v>` = neue Version (z. B. `0.2.43-beta`), `<alt>` = vorige
Version, `<host>` = Testmaschine. Exchange Online ist für diese Prüfung nicht
nötig: Ohne Smarthost stellt das Relay nichts zu und antwortet mit 451.

---

## 0 Paket prüfen

```bash
B=https://github.com/azitc-ac/exo-smtp-relay/releases/download/v<v>
for f in SHA256SUMS SHA256SUMS.sig exo-smtp-relay-<v>-linux.tar.gz exo-smtp-relay-<v>-windows.zip \
         exo-smtp-relay-<v>-windows-offline.zip; do
  curl -sSfLO $B/$f; done
sha256sum -c SHA256SUMS
```

Erwartet: alle drei Pakete `OK`. Unter Windows:
`(Get-FileHash .\exo-smtp-relay-<v>-windows.zip).Hash` mit `SHA256SUMS` vergleichen.

## 1 Neuinstallation

| Plattform | Weg | Weboberfläche |
|---|---|---|
| Windows-Dienst | ZIP entpacken, `windows\install.ps1 -Unattended` als Administrator | `https://<host>:8443` |
| systemd | Schritte im Kopf von `linux/exo-smtp-relay.service` | `https://<host>:8080` |
| Docker | `docker-compose.yml` mit `image: ghcr.io/azitc-ac/exo-smtp-relay:<kanal>`, `docker compose up -d` | `https://<host>:8443` |

**Windows** — auf einem frischen Windows Server ohne Python:
```powershell
powershell.exe -ExecutionPolicy Bypass -File .\windows\install.ps1 -Unattended   # Exit 0
Get-Service ExoSmtpRelay                                    # Running, Automatic
Get-NetTCPConnection -State Listen -LocalPort 25, 8443
Get-NetFirewallRule -DisplayName 'EXO SMTP Relay*'          # zwei Regeln
Get-ChildItem Cert:\LocalMachine\AuthRoot | Measure-Object  # VOR jedem Browser-/Update-Aufruf notieren
```
Die Zahl der Wurzeln (nur Bestandsaufnahme): Windows lädt Wurzelzertifikate erst bei
Bedarf nach. Wer vorher im Browser auf GitHub war, sieht einen Fehler im
Update-Weg nicht.

**systemd**
```bash
systemctl is-active exo-smtp-relay exo-smtp-relay-update.path    # active, active
systemctl is-enabled exo-smtp-relay exo-smtp-relay-update.path   # enabled, enabled
sudo ls -ld /opt/exo-smtp-relay/data                             # drwx------ exo-relay
```

**Docker** — mit einem Verzeichnis `./data`, das es vorher **nicht** gab:
```bash
docker compose up -d && sleep 30
docker ps --format '{{.Names}} {{.Status}}'    # Up …, nicht Restarting
ls -ldn data                                   # 1000 1000
docker exec exo-smtp-relay grep -E '^(Uid|CapEff)' /proc/1/status   # Uid 1000
```

Alle drei: `curl -sk https://<host>:<port>/health` →
`{"ok":true,"smtp":true,"version":"<v>"}`.

**Windows-Upgrade mit gesperrter Datei** (Labtest 0.2.45): in `data\` eine Datei, die
der Administrator nicht zurücksetzen darf, dann `install.ps1 -Unattended` erneut:
```powershell
$f = 'C:\ProgramData\exo-smtp-relay\data\gesperrt.json'; Set-Content $f '{}'
icacls $f /setowner *S-1-5-18; icacls $f /inheritance:r /grant:r *S-1-5-18:F
powershell.exe -ExecutionPolicy Bypass -File .\windows\install.ps1 -Unattended   # Exit 0
```
Erwartet: `WARNUNG: nicht zurueckgesetzt: gesperrt.json (Exit 5: …)` oder
`WARNUNG: Rechte nicht lesbar: gesperrt.json`, Dienst `Running`. Aufräumen:
`takeown /f $f; icacls $f /reset; Remove-Item $f`.

**Windows-Upgrade, das scheitert**: Paket entpacken, in `app\requirements.lock` eine
Zeile `gibt-es-nicht==0.0.1` anhängen, `install.ps1 -Unattended` → Exit ≠ 0,
„ABBRUCH im Schritt 'Abhaengigkeiten'“, „Vorige Fassung <alt> zurueckgeholt“, „Dienst
laeuft wieder (Fassung <alt>)“; `/health` meldet `<alt>`, kein `.install-alt\` bleibt.
(Beides läuft auch im CI-Job „Installer echt“.)

## 1b Offline-Installation (Windows, ohne Internet)

Frischer Windows Server **ohne Python** und ohne Internetzugang (Netzwerkkarte getrennt
oder ausgehend gesperrt); das Offline-Paket per USB/Freigabe übertragen.

```powershell
(Get-FileHash .\exo-smtp-relay-<v>-windows-offline.zip).Hash        # gegen SHA256SUMS
Expand-Archive .\exo-smtp-relay-<v>-windows-offline.zip C:\temp\relay
powershell.exe -ExecutionPolicy Bypass -File C:\temp\relay\exo-smtp-relay-<v>\windows\install.ps1 -Unattended
```

Erwartet, in dieser Reihenfolge:
- `==> Offline-Installation: Python, Wheels und ExchangeOnlineManagement aus dem Paket - kein Download`
- `OK: python-3.12.10-amd64.exe: Pruefsumme und Signatur (Python Software Foundation) gueltig`
  — **Prüfpunkt:** Auf einem völlig netzlosen Server muss Windows die Kette der Signatur
  (Wurzel „Microsoft Identity Verification Root Certificate Authority 2020“) offline
  bilden können. Scheitert das, bricht der Installer mit `Authenticode-Signatur nicht
  gueltig (UnknownError …)` ab — dann notieren; die Wurzel lässt sich über
  `certutil -addstore Root` nachtragen.
- `OK: Offline: pip --no-index aus offline\wheels`
- `OK: installiert: ExchangeOnlineManagement 3.10.1 (Pruefsumme und Microsoft-Signatur geprueft)`
- Exit 0, Dienst `Running`, `/health` meldet `<v>`.
- `C:\ProgramData\exo-smtp-relay\offline\wheels` vorhanden, `offline\python` **nicht**.
- *Aktualisierung → Installationsart*: „Offline-Paket“, geladen wird `…-windows-offline.zip`.

**Gegenprobe Manipulation:** eine Kopie des Pakets, in `offline\python\` ein Byte des
Installers ändern → `Pruefsumme stimmt nicht … Datei NICHT verwendet`, Abbruch vor der
Python-Installation. Dasselbe für `offline\module\*.nupkg`: WARNUNG, kein Modul.

**Update offline-installiert:** mit Kanal `beta` auf `<v>` aktualisieren (Netz nur zu
GitHub). In `data\update\status.json` „ok“; im Dienstprotokoll kein Zugriff auf
`pypi.org`; `offline\wheels` gehört danach zu `<v>` (`offline\inhalt.json`, Feld `relay`).

Der CI-Job „Offline-Paket“ (`tools/offline_probe.ps1`) prüft das alles außer dem Lauf des
Python-Installers selbst (der Runner hat Python schon) und der netzlosen Signaturkette.

## Lieferkette aktualisieren (Python, ExchangeOnlineManagement, Pakete)

- **Python-Installer**: neue Fassung in `$RelayPythonInstaller` (`windows/install.ps1`):
  Datei laden, `sha256sum` selbst rechnen, MD5 gegen
  `https://www.python.org/api/v2/downloads/release_file/?release=<id>` und die PGP-Signatur
  (`.asc`, Schlüssel laut `python.org/downloads/metadata/pgp`) prüfen. Nur Fassungen, für
  die `requirements.lock` Wheels hat (3.11–3.13).
- **ExchangeOnlineManagement**: `$RelayExoModul` — nupkg von
  `https://www.powershellgallery.com/api/v2/package/ExchangeOnlineManagement/<fassung>`
  laden, SHA256 eintragen, `Dockerfile` (`-RequiredVersion`) mitziehen.
- **Python-Pakete**: Fassungen wie im Gateway in `app/requirements.lock`, dann
  `python3 tools/lock_hashes.py schreiben` und `abdeckung` (jede Zielplattform hat ein Wheel).
- Danach `python3 tools/offline_paket.py vorbereiten <stufe>` — bricht bei jeder
  Abweichung ab.

## 2 Oberfläche

1. `/` ohne Anmeldung → Weiterleitung zu `/auth/login`.
2. Anmeldung `admin`/`admin` → `password_change_required: true`; jede andere
   Seite leitet zu `/einrichtung#step-1`, `/api/…` antwortet 403. Band
   „Vorgabepasswort aktiv“.
3. Kennwort unter 10 Zeichen → 400. Kennwort ändern. `admin` danach → 401,
   neues Kennwort → 200.
4. Seiten `/einrichtung`, `/relay`, `/einstellungen`, `/backup`,
   `/aktualisierung`, `/log` antworten 200. In den Einstellungen ist der
   Abschnitt Let's Encrypt sichtbar (nur ansehen, keine Bestellung).
5. Bänder: vor Abschluss der Einrichtung keine; nach „Einrichtung
   abschließen“ ohne Adressen „Keine Postfachadressen bekannt“; Relay
   abgeschaltet → „Relay abgeschaltet“.
6. *Aktualisierung → Prüfen*: laufende Fassung als aktuell, im Kanal `beta`
   zusätzlich `<v>`. `betriebsart` stimmt; `bereit` ist unter Windows und
   systemd `true`, unter Docker `false` mit Hinweis.

## 3 SMTP-Antworten

Alle Antworten müssen ASCII und lesbar sein (Umlaute umgeschrieben).

| Lage | Erwartet |
|---|---|
| Gerät nicht in der Liste, ohne TLS | `530 Must issue a STARTTLS command first` |
| Gerät nicht in der Liste, mit STARTTLS | `554 5.7.1 Access denied` |
| freigegeben, keine Adressen bekannt | `451 4.3.2 Relay temporaer nicht verfuegbar` |
| Absenderdomäne fremd | `550 5.7.1 Absenderdomaene …` |
| Empfänger außerhalb | `550 5.7.1 Relay nur an Empfaenger …` |
| zulässig, kein Smarthost | `451 4.4.1 Zustellung an Exchange Online fehlgeschlagen …` |

Gerät unter *Relay* aus den Abweisungen übernehmen, Adressen von Hand
(`@example.org`) unter *Einstellungen → Adressquelle*.
```bash
swaks --server <host> --port 25 --from druck@example.org --to a@example.org
```
Danach steht das Band „Zustellfehler“ oben; das ist richtig.

## 4 Backup

1. Eine Einstellung ändern (z. B. `LOG_RETENTION_DAYS` = 31).
2. Backup mit Passwort erstellen und herunterladen. Passwort unter 8 Zeichen →
   400. Die Datei beginnt mit `EXORELAY`, nicht mit `PK`. Der Abruf geht genau
   einmal, der zweite antwortet 404.
3. Falsches Passwort beim Ansehen → „Passwort falsch oder Datei beschädigt“.
4. Einstellung erneut ändern (45), Backup wiederherstellen, **Dienst neu
   starten** (`Restart-Service ExoSmtpRelay` / `systemctl restart
   exo-smtp-relay` / `docker restart exo-smtp-relay`).
5. Die Einstellung steht wieder auf 31, Anmeldung klappt.

## 5 Aktualisierung (Tausch)

Ausgang ist eine Installation von `<alt>`, Kanal `beta`.

**Vorher und nachher** den Datenstand vergleichen, er muss gleich sein:
```bash
sudo python3 - <<'PY'
import hashlib, pathlib, sqlite3
d = pathlib.Path("/opt/exo-smtp-relay/data")     # Windows: C:\ProgramData\exo-smtp-relay\data
print(hashlib.sha256((d / "settings.json").read_bytes()).hexdigest())
for n in ("relay_hosts.db", "mail_audit.db"):
    c = sqlite3.connect(f"file:{d / n}?mode=ro", uri=True)
    print(n, hashlib.sha256("\n".join(c.iterdump()).encode()).hexdigest())
PY
```

**a) Gefälschtes Paket** (systemd): Paket entpacken, eine Datei ändern, neu
packen, `SHA256SUMS` anpassen, `SHA256SUMS.sig` unverändert lassen. Alle drei
nach `data/update/` legen, dann `data/update/auftrag.json` schreiben:
```json
{"version":"<v>","paket":"exo-smtp-relay-<v>-linux.tar.gz","angefordert_von":"test","protokoll":1}
```
Erwartet in `data/update/status.json`: `fehler`, „Signatur der Prüfsummen
ungültig …“. `<alt>` läuft weiter, kein Verzeichnis `.update-neu` bleibt liegen.

**b) Rückfall**: Start nur für `<v>` verhindern, etwa mit einem Drop-in
```ini
[Service]
ExecStartPre=/bin/sh -c "! grep -q <v> /opt/exo-smtp-relay/VERSION"
```
Unter Windows leistet dasselbe eine Datei `venv\Lib\site-packages\sitecustomize.py`:
```python
import os, sys
if any("main.py" in a for a in sys.orig_argv) and \
        open(r"C:\ProgramData\exo-smtp-relay\VERSION").read().startswith("<v>"):
    os._exit(3)
```
Einspielen. Erwartet nach rund drei Minuten: `zurueckgerollt`, „… kam nicht
stabil hoch“. `VERSION` und `/health` melden `<alt>`, der Datenstand ist gleich.
Solange der Rückfall läuft, ist das Relay nicht erreichbar.

**c) Tausch**: Blockade entfernen (`systemctl daemon-reload`), erneut einspielen.
Erwartet nach rund einer halben Minute: `ok`, „`<v>` läuft (vorher `<alt>`)“,
`lebenszeichen.json` mit `<v>`, Band „Aktualisierung verfügbar“ weg, Datenstand
gleich, Dienste `active`.

**Docker** tauscht sich nicht selbst:
```bash
docker compose pull && docker compose up -d
docker inspect exo-smtp-relay --format '{{.Config.Image}} {{.State.Health.Status}}'
sha256sum data/settings.json      # gleich wie vorher
```

**Windows, Bestand ohne nachgeladene Wurzeln** (Fassungen bis 0.2.42): Die
Prüfung kann dort mit `CERTIFICATE_VERIFY_FAILED` scheitern. Dann `install.ps1`
aus dem neuen Paket ausführen — einmal, danach geht der Knopf.

**Windows, Upgrade per `install.ps1`** (ab 0.2.44): darf nichts verlieren. Vorher
eine Relay-Regel einschränken, einen eigenen Eintrag in die Dienst-Umgebung setzen
und den Stand festhalten:
```powershell
Set-NetFirewallRule -DisplayName 'EXO SMTP Relay - SMTP 25' -RemoteAddress 10.0.0.0/8 -Profile Domain
Get-NetFirewallRule -DisplayName 'EXO SMTP Relay*' | Get-NetFirewallAddressFilter   # RemoteAddress notieren
(Get-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Services\ExoSmtpRelay).Environment   # notieren
(Get-Acl C:\ProgramData\exo-smtp-relay\data).Access | Format-Table IdentityReference, FileSystemRights, IsInherited
```
Dann `install.ps1` ohne `-WebPort` ausführen. Erwartet: weiterhin genau zwei
Regeln, RemoteAddress und Profil unverändert; der bisherige Web-Port bleibt;
eigene Einträge in `Environment` bleiben; die Schlussmeldung sagt „Aktualisiert.“
und bei eigenem Kennwort „Kennwort unveraendert“. Mit `-WebPort <neu>`: genau
eine Web-Regel, auf dem neuen Port, Einschränkungen unverändert.
`data\logs\service.log` zeigt Umlaute korrekt (erst nach einem Neustart des Dienstes).

**Windows, Rechte auf `data\` werden erzwungen** (ab 0.2.45). Vorher fremde
Einträge setzen, auch auf Unterobjekten:
```powershell
$d = 'C:\ProgramData\exo-smtp-relay\data'
icacls $d /grant '*S-1-5-32-551:(OI)(CI)(R)'                  # Backup Operators: Read
icacls "$d\logs" /inheritance:d; icacls "$d\logs" /grant '*S-1-5-32-545:(OI)(CI)(M)'
```
`install.ps1` ausführen. Erwartet: Ausgabe „entfernt: … Backup Operators Allow Read“,
„entfernt auf logs: …“ und „Vererbung wieder an: logs“, Schluss „data\ nur fuer SYSTEM
und Administratoren lesbar (n fremde Eintraege entfernt, m Unterobjekte zurueckgesetzt)“.
Danach:
```powershell
(Get-Acl $d).Access | Format-Table IdentityReference, FileSystemRights, IsInherited   # genau SYSTEM + Administratoren, nicht geerbt
(Get-Acl $d).AreAccessRulesProtected                                                  # True
Get-ChildItem $d -Recurse -Force | Where-Object { (Get-Acl $_.FullName).Access | Where-Object { -not $_.IsInherited } }   # leer
Get-Service ExoSmtpRelay                                                              # Running (LocalSystem hat Zugriff)
```

**Windows, Portwechsel mit eingeschränkter Web-Regel** (ab 0.2.45). Die Web-Regel
einschränken und eine zweite, offene Web-Regel auf dem Zielport anlegen:
```powershell
Set-NetFirewallRule -DisplayName 'EXO SMTP Relay - Web 8443' -RemoteAddress 10.0.0.0/8 -Profile Domain
New-NetFirewallRule -DisplayName 'EXO SMTP Relay - Web 9443' -Direction Inbound -Protocol TCP -LocalPort 9443 -Action Allow
.\windows\install.ps1 -WebPort 9443
Get-NetFirewallRule -DisplayName 'EXO SMTP Relay - Web*' | ForEach-Object {
  $_.DisplayName, $_.Profile, ($_ | Get-NetFirewallAddressFilter).RemoteAddress, ($_ | Get-NetFirewallPortFilter).LocalPort }
```
Erwartet: genau eine Web-Regel „Web 9443“, Port 9443, RemoteAddress 10.0.0.0/8, Profil
Domain — die eingeschränkte bleibt, die offene ist weg. Ohne die offene Regel dasselbe.

## 6 Festhalten

Je Plattform: Version, Kanal, Anzahl der Prüfungen, Abweichungen mit Log-Zeile.
Fehler auf frischen Systemen immer nachstellen, bevor ein Fix als bestätigt gilt:
**ohne** den Fix rot, **mit** dem Fix grün.

## 7 Selbstsigniertes Zertifikat

Frisch ausgestellt (nicht bei einer bestehenden Installation) trägt es unter
Windows und systemd den Rechnernamen als SAN, damit der Startmenü-Link passt; im
Container nur `exo-smtp-relay.local`:
```bash
echo | openssl s_client -connect <host>:<port> 2>/dev/null | openssl x509 -noout -ext subjectAltName
```
