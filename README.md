# EXO SMTP Relay

Ein schlanker SMTP-Relay-Dienst für die Geräte und Anwendungen in deinem Netz —
Drucker, Scanner, Fachanwendungen —, die bisher anonym bei einem lokalen
Exchange-Server abgeliefert haben. Der Dienst nimmt ihre Post auf Port 25
entgegen, prüft Gerät, Absender und Ziel und übergibt sie an Exchange Online.

Er ist die Auskopplung des SMTP-Relays aus dem
[EXO Signature Gateway](https://github.com/azitc-ac/EXO-Signature-Gateway):
dieselben Regeln, dieselbe Geräteliste, derselbe Lernmodus — ohne Signaturen,
S/MIME, ACME und Graph. Du betreibst ihn als Docker-Container (amd64/arm64), als
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

Ein Relay, das zu viel durchlässt, fällt nicht auf den Dienst zurück, sondern
auf deinen Ruf. Deshalb gelten drei Grenzen, inhaltsgleich mit dem Gateway
(`app/smtp_relay.py`):

1. **Gerät** — nur Adressen aus deiner Geräteliste dürfen einliefern. Ein Netz
   ist keine Freigabe; es sagt nur, woraus der Lernmodus lernen darf.
2. **Absender** — nur Domänen, die deinem Tenant gehören. Ein übernommener
   Drucker kann nicht als fremde Firma versenden.
3. **Ziel** — Vorgabe: nur Empfänger in deinem Tenant, je Gerät umstellbar.
   Geprüft wird gegen die *Adressen*, nicht gegen die Domäne: eine unbekannte
   Adresse deiner eigenen Domäne ergäbe einen Unzustellbarkeitsbericht nach
   aussen.

**Lernmodus:** Kennst du nicht alle Geräte, gib einen Bereich (`192.168.1.0/24`
oder `172.16.16.10-172.16.17.20`) für höchstens zwei Stunden frei. Jedes Gerät,
das darin etwas Zulässiges einliefert, landet in deiner Liste. Ausserhalb des
Zeitfensters lässt der Bereich nichts durch.

**Ausfallrichtung:** Kennt der Dienst die Postfachadressen deines Tenants nicht,
weist er jede Einlieferung mit `451` ab — nicht durch. Scheitert die Übergabe an
Exchange, antwortet er ebenfalls mit `451`; das Gerät versucht es erneut, und
du verlierst nichts.

---

## Was du auf Exchange-Seite brauchst

| Was | Wozu | Pflicht |
|---|---|---|
| **Inbound-Connector** (OnPremises) | Exchange nimmt Post vom Relay für beliebige Absender deiner Domänen an | ja |
| **App-Registrierung** mit `Exchange.ManageAsApp` + Rolle *Exchange-Administrator* + Zertifikat | Postfachliste abrufen, Connector anlegen | empfohlen |
| Ausgehend **Port 25** zum Smarthost `<domäne>.mail.protection.outlook.com` | Rückweg | ja, ausser Modus 587 |

Der Inbound-Connector erkennt dein Relay **am TLS-Zertifikat** (nur mit einem
Zertifikat einer öffentlichen CA) **oder an deiner öffentlichen IP** (feste
Adresse nötig — mit selbstsigniertem Zertifikat der einzige Weg). Beides richtet
dir die Oberfläche ein. Willst du es selbst tun, nimm
`app/scripts/setup_relay_connector.ps1`; das Skript läuft auf jedem
Windows-Rechner mit PowerShell 5.1 und dem Modul ExchangeOnlineManagement.

Ohne App-Registrierung geht es auch: Trag die Postfachadressen von Hand unter
*Einstellungen → Adressquelle* ein. Ihre Domänen gelten dann als deine eigenen.
Betreibst du das grosse Gateway, kannst du dessen `auth.pfx` und App-ID
übernehmen.

---

## Installation

### Docker (Linux, Raspberry Pi)

```bash
git clone https://github.com/azitc-ac/exo-smtp-relay.git && cd exo-smtp-relay
docker compose up -d --build
```

Die Weboberfläche erreichst du unter `https://<host>:8443` (selbstsigniert),
Anmeldung `admin` / `admin` — ändere das beim ersten Aufruf. Port 25 ist im
Container freigegeben; das Abbild enthält PowerShell 7 und das
ExchangeOnlineManagement-Modul.

### Windows-Dienst

Du brauchst Python 3.11 oder neuer (python.org, „Add python.exe to PATH").

```powershell
# als Administrator, im entpackten Verzeichnis
.\windows\install.ps1
```

Der Installer kopiert die Anwendung nach `C:\ProgramData\exo-smtp-relay`, legt
eine venv an, registriert den Dienst **ExoSmtpRelay** (Autostart), öffnet die
Firewall für Port 25 und den Web-Port und bietet dir die Installation des
ExchangeOnlineManagement-Moduls an. Er läuft unter Windows PowerShell 5.1 und
PowerShell 7. Entfernen kannst du alles mit `.\windows\uninstall.ps1`.

Ist Port 25 belegt (IIS-SMTP, ein Virenscanner, ein anderes Relay), nennt dir
der Installer den Prozess. Der Dienst startet erst, wenn der Port frei ist.

### systemd (ohne Docker)

Sieh dir `linux/exo-smtp-relay.service` an — die Unit erklärt die Schritte im
Kopf. Für die Postfachabfrage installierst du `pwsh` getrennt.

---

## Einrichtung — wenige Klicks

Nach der Anmeldung führt dich die Startseite zum **Einrichtungsassistenten**
(`/einrichtung`), bis du ihn abgeschlossen hast. Sechs Schritte, jeder mit
sichtbarem Zustand:

1. **Adminzugang sichern** — dein eigenes Passwort.
2. **Hostname** — der Name, unter dem Exchange deinen Dienst kennt. Das
   TLS-Zertifikat wird selbstsigniert darauf ausgestellt.
3. **Entra-Login** — melde dich einmal als Entra-Administrator an. Im
   Hintergrund legt der Dienst die App-Registrierung an (nur
   `Exchange.ManageAsApp`, kein Geheimnis), erteilt die Zustimmung, weist die
   Rolle Exchange-Administrator zu, erzeugt das Auth-Zertifikat und lädt es
   hoch, erkennt Tenant und Smarthost und holt die Postfachliste. Für den Login
   dient eine kleine Login-App (Public Client); betreibst du das Gateway, trag
   dessen „… Login"-App ein.
4. **Inbound-Connector** — Zertifikat- oder IP-Variante, per PowerShell.
5. **Geräte** — starte den Lernmodus und löse an jedem Gerät einen Testversand
   aus, oder trag die Geräte von Hand ein.
6. **Abschluss** — danach führt dich die Startseite zum Dashboard.

Alles, was der Assistent setzt, änderst du später unter *Einstellungen*. Ohne
Entra-Login geht es auch: App-ID, Tenant und Zertifikat von Hand, oder ganz ohne
App-Registrierung mit Adressen von Hand.

---

## Betrieb

Vier Seiten: **Dashboard**, **Einrichtung**, **Einstellungen**, **Protokolle**.

- **Dashboard**: Zähler für den gewählten Zeitraum (heute / 7 / 30 / 90 Tage);
  je Gerät die Einlieferungen mit **TLS** bzw. **Klartext** und an **interne**
  bzw. **externe** Empfänger, dazu Abgelehntes und das Aufkommen der letzten
  30/90/180/360 Tage. Hier sperrst, kommentierst und gibst du Geräte frei;
  abgewiesene Adressen siehst du mit Absender, Ziel und Grund — *Übernehmen*
  genügt für ein neues Gerät; Lernmodus; letzte Einlieferungen.
- **Einstellungen**: Rückweg, Tenant und App, Adressquelle, Connector,
  Zertifikate, Anmeldung, Betrieb.
- **Protokolle**: Live-Protokoll und Suche; jede Nachricht trägt eine
  `[mail:…]`-Trace-ID.
- **Daten**: alles liegt unter `data/` (Einstellungen, Geräteliste,
  Mail-Protokoll, Zertifikate) — Rechte 600/700; unter Windows nur SYSTEM und
  Administratoren.

Über die Umgebung setzt du nur Startwerte, danach gilt `settings.json`:
`DATA_DIR`, `SMTP_PORT`, `WEBUI_PORT`, `PWSH` (Pfad zur PowerShell),
`TENANT_DOMAIN`, `CLIENT_ID`, `EXO_SMARTHOST`, `WEBUI_USERNAME`, `WEBUI_PASSWORD`.

---

## Verhältnis zum EXO Signature Gateway

Die Regeln (`smtp_relay.py`), die Geräteliste (`relay_hosts.py`) und einige
Bausteine sind **geprüfte Kopien** aus dem Gateway — kein gemeinsames Paket,
damit du jeden Dienst für sich installieren kannst. Was das Relay bewusst
**nicht** hat: Signaturen, S/MIME, ACME, Graph im Betrieb, Microsoft-Login,
Hub-Anbindung. Brauchst du das, betreib das Gateway — dessen Relay ist
dasselbe.

### Spiegelung — so kommen Änderungen aus dem Gateway herüber

Nichts kommt von allein. Drei Werkzeuge halten dir die Kopien gleich:

| Werkzeug | Was es tut |
|---|---|
| `tools/driftcheck.py` | **Meldet** dir Abweichungen der zehn gespiegelten Dateien (SHA-256), wenn das Gateway daneben liegt (`../EXO-Signature-Gateway`). Läuft in der Testsuite mit. |
| `tools/spiegel_holen.py` | **Zeigt** dir je abweichender Datei den letzten Gateway-Commit und **übernimmt** sie mit `--uebernehmen`. Kopiert nur Gateway → Relay; ist die Datei im Relay neuer, sagt es dir das und lässt sie stehen. |
| `.github/workflows/spiegel.yml` | **Nächtlich**: checkt das öffentliche Gateway aus, vergleicht, und öffnet dir bei Abweichung einen Pull Request mit den aktualisierten Kopien samt Testergebnis. Kein Secret nötig. |

Damit der Workflow den PR anlegen darf, setz in den Repo-Einstellungen unter
*Settings → Actions → General → Workflow permissions* den Haken „Allow GitHub
Actions to create and approve pull requests". Ohne ihn bleibt der Lauf bei der
Abweichung stehen und zeigt sie dir nur.

Was **nicht** gespiegelt wird und im Relay eigens gebaut werden muss: der
Handler, das Dashboard, die Einrichtung, die Einstellungen. Ein neues Feature,
das im Gateway die Verdrahtung oder die Oberfläche berührt, kommt hier nicht
von selbst an — nur seine Regeln, wenn sie in `smtp_relay.py` oder
`relay_hosts.py` liegen.

Die Gegenrichtung (Änderung im Relay-Kern → Gateway) machst du von Hand im
Gateway-Repo; `spiegel_holen.py` weist dich darauf hin.

### Herkunft

Der Dienst entstand als Auskopplung aus dem Gateway
([Pull Request #1](https://github.com/azitc-ac/EXO-Signature-Gateway/pull/1)),
in dem du die Entstehung Schritt für Schritt nachlesen kannst. Seit v0.2.0 lebt
er in diesem eigenen Repository.

---

## Entwicklung

```bash
pip install -r app/requirements.lock -r tests/requirements.txt
pytest tests/ -v
python tools/driftcheck.py          # Spiegelung gegen das Gateway prüfen
python tools/spiegel_holen.py       # Abweichungen zeigen, --uebernehmen kopiert
cd app && DATA_DIR=../data SMTP_PORT=2525 WEBUI_PORT=8080 python main.py
```

Speichere PowerShell-Skripte **mit BOM** und halte sie **PowerShell 5.1**-
tauglich; `tests/test_ps_skripte.py` prüft beides, die Windows-CI parst sie mit
5.1.

## Lizenz

Siehe `LICENSE.md` — PolyForm Internal Use, Community Edition wie beim Gateway.
