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
| **App-Registrierung** mit `Exchange.ManageAsApp` + Zertifikat — *Exchange-Administrator* nur zum Anlegen des Connectors, danach **nur Lesen** | Postfachliste abrufen, Connector anlegen | empfohlen |
| Ausgehend **Port 25** zum Smarthost `<domäne>.mail.protection.outlook.com` (oder Port 587) | Rückweg zu Exchange | ja |

### Rückweg: smarthost oder submit

Der Relay kann die angenommene Post auf zwei Wegen an Exchange Online übergeben:

**smarthost (Vorgabe)** — Port 25 + TLS-Zertifikat
  - Der Inbound-Connector erkennt dein Relay am TLS-Zertifikat
  - Zertifikat: öffentliche CA (teuer) **oder** IP-basiert mit selbstsigniertem (feste Adresse nötig)
  - Braucht ausgehenden **Port 25** (blockiert in vielen Netzen)
  - Dreier: `app/scripts/setup_relay_connector.ps1` richtet den Connector automatisch ein

**submit** — Port 587 + Authentifizierung
  - Für Standorte **ohne ausgehenden Port 25**
  - Braucht ein **Dienstkonto** in deinem Tenant (kann auch der Gateway-Account sein)
  - Konto muss SMTP AUTH-berechtigt sein und „Senden als" für die Geräte-Absender haben
  - Achtung: Exchange schreibt den Absender auf das Dienstkonto um, sofern keine „Senden als"-Rechte
  - In der Web-Oberfläche unter *Einstellungen → Rückweg* auf „submit" umschalten

Der Inbound-Connector ist **in beiden Modi nötig** — er sagt Exchange, welche Adressen vom Relay kommen dürfen.
Willst du ihn selbst einrichten, nutze `app/scripts/setup_relay_connector.ps1`; das Skript läuft auf jedem
Windows-Rechner mit PowerShell 5.1 und dem Modul ExchangeOnlineManagement.

Ohne App-Registrierung geht es auch: Trag die Postfachadressen von Hand unter
*Einstellungen → Adressquelle* ein. Ihre Domänen gelten dann als deine eigenen.
Betreibst du das grosse Gateway, kannst du dessen `auth.pfx` und App-ID
übernehmen.

---

### Rechte der App — so wenig wie nötig

Die App braucht *Exchange-Administrator* nur, um den Inbound-Connector anzulegen.
Gleich danach stuft der Assistent sie herab: Sie kommt in eine Exchange-Rollengruppe
*„<Name> - nur lesen“* (*View-Only Recipients* + *View-Only Configuration*), und
die Entra-Rolle wird entfernt. Ein kopierter Schlüssel kann dann nur noch die
Postfachliste lesen — keine Connectoren, keine Transportregeln, keine Postfächer
ändern. Für eine spätere Änderung am Connector holst du das Admin-Recht unter
*Einrichtung → Rechte der App* kurz zurück; danach wird wieder herabgestuft.
Dort misst *Rechte jetzt messen* auch, was die App gerade wirklich darf.

Exchange übernimmt das Herabstufen **nicht sofort**: Die Entra-Rolle ist sofort
weg, Exchange lässt die App aber noch eine Weile schreiben. Gemessen wurden
zwischen drei und viereinhalb Stunden (nach 202 Minuten noch Schreibrechte, nach 263 Minuten nicht
mehr); eine Frist nennt Microsoft nicht. Der Dienst misst deshalb
stündlich nach und zeigt unter *Einrichtung → Rechte der App*, ab wann die
Schreibrechte wirklich weg sind und nach wie vielen Minuten.

Der Schlüssel der App gilt **ein Jahr** und wird 30 Tage vor Ablauf selbst
erneuert (Graph `addKey`/`removeKey` — dafür braucht die App kein zusätzliches
Recht). Der alte Schlüssel wird im nächsten stündlichen Lauf ausgetragen, nicht
sofort: Entra kennt den neuen in den ersten Minuten noch nicht überall, und das
Austragen muss mit ihm belegt werden. Gelingt es einen Tag lang nicht, steht das
unter *Einrichtung → Schlüssel der App*.
Ist er doch einmal abgelaufen, genügt eine neue Anmeldung im Assistenten.

Das gilt nur für eine App, die der Assistent selbst angelegt hat. Nutzt das
Relay über ein importiertes Zertifikat die App des Signatur-Gateways mit, fasst
es deren Rechte und Schlüssel nicht an — das Gateway braucht im Betrieb
Schreibrechte.

### Betrieb ganz ohne App-Registrierung

Wer keinen Schlüssel mit Tenant-Rechten auf dem Server haben will:

1. **Adressen von Hand** (*Einstellungen → Adressquelle*), *Stündlich abrufen*
   aus. Eine Zeile `@firma.de` je Domäne genügt: Jede Adresse dieser Domäne
   gilt dann als internes Ziel. Gröber als die abgefragte Liste — auch
   Adressen, die es nicht gibt, gelten als intern; Exchange weist sie selbst ab.
   Willst du es genau, trägst du stattdessen die einzelnen Adressen ein.
2. **Connector selbst anlegen**, als Administrator in einer eigenen
   PowerShell-Sitzung (Zertifikatsvariante; bei fester IP statt
   `-RequireTls`/`-TlsSenderCertificateName` die Option `-SenderIPAddresses`):

   ```powershell
   Connect-ExchangeOnline
   New-InboundConnector -Name "EXO SMTP Relay - Inbound" -ConnectorType OnPremises `
       -SenderDomains @("*") -RequireTls $true `
       -TlsSenderCertificateName "relay.firma.de" -Enabled $true
   ```
3. Im Assistenten Schritt 3 und 4 überspringen.

Auf dem Server liegt dann nur das TLS-Zertifikat des Relays — kein Schlüssel,
mit dem sich jemand bei Microsoft 365 anmelden könnte.

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

Öffne PowerShell **als Administrator** im entpackten Verzeichnis:

```powershell
.\windows\install.ps1
```

Das Skript:
- **Prüft Python 3.11+** — falls nicht vorhanden, bietet automatischen Download und Installation von python.org
- Kopiert die Anwendung nach `C:\ProgramData\exo-smtp-relay`
- Legt eine Python venv an und installiert Abhängigkeiten
- Registriert den Dienst **ExoSmtpRelay** mit Autostart
- Öffnet die Firewall für Port 25 (eingehend von Geräten) und den Web-Port
- Bietet die Installation des PowerShell-Moduls **ExchangeOnlineManagement** an (optional, aber für den Einrichtungsassistenten nötig)

Der Installer läuft unter **Windows PowerShell 5.1** und PowerShell 7.

**Deinstallation:** `.\windows\uninstall.ps1`

**Hinweis:** Ist Port 25 bereits belegt (IIS-SMTP, Virenscanner, anderer Mail-Relay), nennt dir
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
   Rolle Exchange-Administrator zu (nur bis der Connector steht, siehe
   *Rechte der App*), erzeugt das Auth-Zertifikat und lädt es
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
