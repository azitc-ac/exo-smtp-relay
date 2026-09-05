# EXO SMTP Relay

Ein schlanker SMTP-Relay-Dienst für Geräte und Anwendungen im eigenen Netz —
Drucker, Scanner, Fachanwendungen —, die bisher anonym bei einem lokalen
Exchange-Server abgeliefert haben. Der Dienst nimmt ihre Post auf Port 25
entgegen, prüft Gerät, Absender und Ziel und übergibt sie an Exchange Online.

Er ist die Auskopplung des SMTP-Relays aus dem
[EXO Signature Gateway](https://github.com/azitc-ac/EXO-Signature-Gateway):
dieselben Regeln, dieselbe Geräteliste, derselbe Lernmodus — ohne Signaturen,
S/MIME, ACME und Graph. Läuft als Docker-Container (amd64/arm64), als
systemd-Dienst oder als **Windows-Dienst**.

```
Drucker / Scanner / Anwendung
        │  SMTP :25 (STARTTLS angeboten, für Geräte nicht Pflicht)
        ▼
  EXO SMTP Relay  ──  Gerät in der Liste?  Absenderdomäne eigen?  Ziel zulässig?
        │  SMTP :25 + STARTTLS  (oder :587 mit Konto)
        ▼
  Exchange Online  ──  Inbound-Connector  ──  Zustellung
```

---

## Die drei Grenzen

Ein Relay, das zu viel durchlässt, macht dem Betreiber Ärger, den er nicht dem
Dienst zuschreibt, sondern seinem Ruf. Deshalb gilt, inhaltsgleich mit dem
Gateway (`app/smtp_relay.py`):

1. **Gerät** — nur Adressen aus der Geräteliste dürfen einliefern. Ein Netz ist
   keine Freigabe; es sagt nur, woraus der Lernmodus lernen darf.
2. **Absender** — nur Domänen, die dem Tenant gehören. Ein übernommener Drucker
   kann nicht als fremde Firma versenden.
3. **Ziel** — Vorgabe: nur Empfänger im eigenen Tenant, je Gerät umstellbar.
   Geprüft wird gegen die *Adressen*, nicht gegen die Domäne: eine unbekannte
   Adresse der eigenen Domäne ergäbe einen Unzustellbarkeitsbericht nach aussen.

**Lernmodus:** Wer nicht alle Geräte kennt, gibt einen Bereich (`192.168.1.0/24`
oder `172.16.16.10-172.16.17.20`) für höchstens zwei Stunden frei. Jedes Gerät,
das darin etwas Zulässiges einliefert, wird in die Liste aufgenommen. Ausserhalb
des Zeitfensters lässt der Bereich nichts durch.

**Ausfallrichtung:** Kennt der Dienst die Postfachadressen des Tenants nicht,
weist er jede Einlieferung mit `451` ab — nicht durch. Scheitert die Übergabe an
Exchange, antwortet er ebenfalls mit `451`; das Gerät versucht es erneut, und
nichts geht verloren.

---

## Voraussetzungen auf Exchange-Seite

| Was | Wozu | Pflicht |
|---|---|---|
| **Inbound-Connector** (OnPremises) | Exchange nimmt Post vom Relay für beliebige Absender der eigenen Domänen an | ja |
| **App-Registrierung** mit `Exchange.ManageAsApp` + Rolle *Exchange-Administrator* + Zertifikat | Postfachliste abrufen, Connector anlegen | empfohlen |
| Ausgehend **Port 25** zum Smarthost `<domäne>.mail.protection.outlook.com` | Rückweg | ja, ausser Modus 587 |

Der Inbound-Connector erkennt das Relay **am TLS-Zertifikat** (nur mit einem
Zertifikat einer öffentlichen CA) **oder an der öffentlichen IP** (feste Adresse
nötig — mit selbstsigniertem Zertifikat der einzige Weg). Beides richtet die
Oberfläche ein; wer es selbst tun will, nimmt `app/scripts/setup_relay_connector.ps1`
(läuft auf jedem Windows-Rechner mit PowerShell 5.1 und dem Modul
ExchangeOnlineManagement).

Ohne App-Registrierung geht es auch: Postfachadressen von Hand unter
*Einstellungen → Adressquelle* eintragen. Ihre Domänen gelten dann als eigene.
Wer das grosse Gateway betreibt, kann dessen `auth.pfx` und App-ID übernehmen.

---

## Installation

### Docker (Linux, Raspberry Pi)

```bash
git clone https://github.com/azitc-ac/exo-smtp-relay.git && cd exo-smtp-relay
docker compose up -d --build
```

Weboberfläche: `https://<host>:8443` (selbstsigniert), Anmeldung `admin` / `admin`
— beim ersten Aufruf ändern. Port 25 ist im Container freigegeben; das Abbild
enthält PowerShell 7 und das ExchangeOnlineManagement-Modul.

### Windows-Dienst

Voraussetzung: Python 3.11 oder neuer (python.org, „Add python.exe to PATH").

```powershell
# als Administrator, im entpackten Verzeichnis
.\windows\install.ps1
```

Der Installer kopiert die Anwendung nach `C:\ProgramData\exo-smtp-relay`, legt
eine venv an, registriert den Dienst **ExoSmtpRelay** (Autostart), öffnet die
Firewall für Port 25 und den Web-Port und bietet die Installation des
ExchangeOnlineManagement-Moduls an. Läuft unter Windows PowerShell 5.1 und
PowerShell 7. Entfernen mit `.\windows\uninstall.ps1`.

Ist Port 25 belegt (IIS-SMTP, ein Virenscanner, ein anderes Relay), meldet der
Installer den Prozess. Der Dienst startet erst, wenn der Port frei ist.

### systemd (ohne Docker)

Siehe `linux/exo-smtp-relay.service` — die Unit erklärt die Schritte im Kopf.
Für die Postfachabfrage wird `pwsh` getrennt installiert.

---

## Einrichtung — wenige Klicks

Die Startseite führt nach der Anmeldung zum **Einrichtungsassistenten**
(`/einrichtung`), bis dieser abgeschlossen ist. Sechs Schritte, jeder mit
sichtbarem Zustand:

1. **Adminzugang sichern** — eigenes Passwort.
2. **Hostname** — der Name, unter dem Exchange den Dienst kennt. Das
   TLS-Zertifikat wird selbstsigniert darauf ausgestellt.
3. **Entra-Login** — einmal als Entra-Administrator anmelden. Im Hintergrund
   legt der Dienst die App-Registrierung an (nur `Exchange.ManageAsApp`, kein
   Geheimnis), erteilt die Zustimmung, weist die Rolle Exchange-Administrator
   zu, erzeugt das Auth-Zertifikat und lädt es hoch, erkennt Tenant und
   Smarthost und holt die Postfachliste. Für den Login dient eine kleine
   Login-App (Public Client); wer das Gateway betreibt, trägt dessen
   „… Login"-App ein.
4. **Inbound-Connector** — Zertifikat- oder IP-Variante, per PowerShell.
5. **Geräte** — Lernmodus starten und an jedem Gerät einen Testversand
   auslösen, oder Geräte von Hand eintragen.
6. **Abschluss** — danach führt die Startseite zum Dashboard.

Alles, was der Assistent setzt, lässt sich unter *Einstellungen* ändern. Ohne
Entra-Login geht es auch: App-ID, Tenant und Zertifikat von Hand, oder ganz ohne
App-Registrierung mit Adressen von Hand.

---

## Betrieb

Vier Seiten: **Dashboard**, **Einrichtung**, **Einstellungen**, **Protokolle**.

- **Dashboard**: Zähler für den gewählten Zeitraum (heute / 7 / 30 / 90 Tage);
  je Gerät die Einlieferungen mit **TLS** bzw. **Klartext** und an **interne**
  bzw. **externe** Empfänger, dazu Abgelehntes und das Aufkommen der letzten
  30/90/180/360 Tage. Geräte sperren, kommentieren, freigeben; abgewiesene
  Adressen mit Absender, Ziel und Grund — *Übernehmen* genügt für ein neues
  Gerät; Lernmodus; letzte Einlieferungen.
- **Einstellungen**: Rückweg, Tenant und App, Adressquelle, Connector,
  Zertifikate, Anmeldung, Betrieb.
- **Protokolle**: Live-Protokoll und Suche; jede Nachricht trägt eine
  `[mail:…]`-Trace-ID.
- **Daten**: alles unter `data/` (Einstellungen, Geräteliste, Mail-Protokoll,
  Zertifikate) — Rechte 600/700; unter Windows nur SYSTEM und Administratoren.

Einstellungen über die Umgebung (nur Startwerte, danach gilt `settings.json`):
`DATA_DIR`, `SMTP_PORT`, `WEBUI_PORT`, `PWSH` (Pfad zur PowerShell),
`TENANT_DOMAIN`, `CLIENT_ID`, `EXO_SMARTHOST`, `WEBUI_USERNAME`, `WEBUI_PASSWORD`.

---

## Verhältnis zum EXO Signature Gateway

Die Regeln (`smtp_relay.py`), die Geräteliste (`relay_hosts.py`) und einige
Bausteine sind **geprüfte Kopien** aus dem Gateway — kein gemeinsames Paket,
damit jeder Dienst für sich installierbar bleibt. Was das Relay bewusst
**nicht** hat: Signaturen, S/MIME, ACME, Graph im Betrieb, Microsoft-Login,
Hub-Anbindung. Wer das braucht, betreibt das Gateway — dessen Relay ist
dasselbe.

### Spiegelung — so kommen Änderungen aus dem Gateway herüber

Nichts kommt von allein. Drei Werkzeuge halten die Kopien gleich:

| Werkzeug | Was es tut |
|---|---|
| `tools/driftcheck.py` | **Meldet** Abweichungen der zehn gespiegelten Dateien (SHA-256), wenn das Gateway daneben liegt (`../EXO-Signature-Gateway`). Läuft in der Testsuite mit. |
| `tools/spiegel_holen.py` | **Zeigt** je abweichender Datei den letzten Gateway-Commit und **übernimmt** sie mit `--uebernehmen`. Kopiert nur Gateway → Relay; ist die Datei im Relay neuer, meldet es das und lässt sie stehen. |
| `.github/workflows/spiegel.yml` | **Nächtlich**: checkt das öffentliche Gateway aus, vergleicht, und öffnet bei Abweichung einen Pull Request mit den aktualisierten Kopien samt Testergebnis. Kein Secret nötig. |

Damit der Workflow den PR anlegen darf, muss in den Repo-Einstellungen unter
*Settings → Actions → General → Workflow permissions* der Haken „Allow GitHub
Actions to create and approve pull requests" gesetzt sein. Ohne ihn bleibt der
Lauf bei der Abweichung stehen und zeigt sie nur.

Was **nicht** gespiegelt wird und im Relay eigens gebaut werden muss: der
Handler, das Dashboard, die Einrichtung, die Einstellungen. Ein neues Feature,
das im Gateway die Verdrahtung oder die Oberfläche berührt, kommt hier nicht
von selbst an — nur seine Regeln, wenn sie in `smtp_relay.py` oder
`relay_hosts.py` liegen.

Die Gegenrichtung (Änderung im Relay-Kern → Gateway) ist Handarbeit im
Gateway-Repo; `spiegel_holen.py` weist darauf hin.

### Herkunft

Der Dienst entstand als Auskopplung aus dem Gateway
([Pull Request #1](https://github.com/azitc-ac/EXO-Signature-Gateway/pull/1)),
in dem die Entstehung Schritt für Schritt nachlesbar ist. Seit v0.2.0 lebt er
in diesem eigenen Repository.

---

## Entwicklung

```bash
pip install -r app/requirements.lock -r tests/requirements.txt
pytest tests/ -v
python tools/driftcheck.py          # Spiegelung gegen das Gateway prüfen
python tools/spiegel_holen.py       # Abweichungen zeigen, --uebernehmen kopiert
cd app && DATA_DIR=../data SMTP_PORT=2525 WEBUI_PORT=8080 python main.py
```

PowerShell-Skripte werden **mit BOM** gespeichert und sind **PowerShell 5.1**-
tauglich; `tests/test_ps_skripte.py` prüft beides, die Windows-CI parst sie mit
5.1.

## Lizenz

Siehe `LICENSE.md` — PolyForm Internal Use, Community Edition wie beim Gateway.
