# Changelog — EXO SMTP Relay

## 0.2.5 — 2026-10-05 — Verwaltung auf eigener Seite „SMTP-Relay", Dashboard zeigt die Auswertung

- **Neue Seite SMTP-Relay** (`/relay`, Menüpunkt *Relay*): Lernmodus, Geräteliste
  (Kommentar, *Extern erlaubt*, Sperren, Löschen, von Hand hinzufügen, Namen
  auflösen) und *Abgewiesen* (Übernehmen, Liste leeren) stehen jetzt dort.
- **Dashboard**: Kennzahlen, das Aufkommen je Gerät (TLS/Klartext, intern/extern,
  30/90/180/360 Tage) und die letzten Einlieferungen — nur zur Ansicht. Läuft der
  Lernmodus oder wurden unbekannte Absender abgewiesen, weist ein Hinweis auf die
  neue Seite.
- Die Geräteliste lädt sich regelmässig neu, aber nicht, solange ein Kommentar
  ungespeichert ist oder gerade getippt wird.
- Für die Anmeldung per OAuth2 im submit-Modus (*SMTP.SendAsApp*) fehlte die
  Bibliothek `msal` in den Abhängigkeiten; sie ist jetzt enthalten, in derselben
  Fassung wie im Gateway.
- Windows-Installer: Umlaute in Meldungen ersetzt, damit Windows PowerShell 5.1 sie
  korrekt anzeigt.

## 0.2.4 — 2026-10-05 — Spiegelabgleich: style.css

- `app/webui/static/style.css` an das Gateway angeglichen. Elemente mit dem Attribut
  `hidden` bleiben jetzt verborgen, auch wenn eine Stilregel (etwa für Knöpfe) ein
  `display` setzt. Die bisherige Sonderregel für die Speicherleiste geht darin auf.
  Im Relay ändert das kein bestehendes Verhalten: Keine Seite schaltet ein Element
  zugleich über `hidden` und über einen Inline-Stil.

## 0.2.3 — 2026-09-08 — Spiegelabgleich: dark-mode.css + style.css

- `app/webui/static/dark-mode.css` und `app/webui/static/style.css` an das
  Gateway angeglichen. Neu im Gateway sind ID-Regeln und ein data-Attribut-Zustand
  für Kästen des Wächter-Installers; die Regeln sind im Relay ungenutzt, die
  Spiegelung verlangt aber Byte-Gleichheit.

## 0.2.2 — 2026-09-07 — Spiegelabgleich: dark-mode.css

- `app/webui/static/dark-mode.css` an das Gateway angeglichen (ID-Regel für einen per JS eingeblendeten Hinweiskasten).

## 0.2.1 — 2026-09-05 — Spiegelabgleich mit dem Gateway

- `tools/spiegel_holen.py`: zeigt je gespiegelter Datei, was im Gateway
  anders ist (letzter Commit), und übernimmt sie mit `--uebernehmen` — nur in
  Richtung Gateway → Relay, nie über eine Relay-Änderung hinweg.
- `.github/workflows/spiegel.yml`: nächtlicher Abgleich gegen das öffentliche
  Gateway-Repo; bei Abweichung ein Pull Request mit den Kopien und dem
  Testergebnis. Ohne Secret.

## 0.2.0 — 2026-09-05 — Einrichtungsassistent und Dashboard

- **Einrichtung** in sechs Schritten wie beim Gateway: Passwort, Hostname
  (TLS-Zertifikat wird darauf ausgestellt), Entra-Login, Connector, Geräte,
  Abschluss. Der Entra-Login legt im Hintergrund die App-Registrierung an
  (nur `Exchange.ManageAsApp`, kein Geheimnis), erteilt die Zustimmung, weist
  die Rolle Exchange-Administrator zu, erzeugt das Auth-Zertifikat, lädt es
  hoch, erkennt Tenant und Smarthost und trägt die Rückadresse an der
  Login-App nach. Die Startseite führt dorthin, bis der Abschluss geklickt ist.
- **Dashboard** statt Übersicht und Geräteseite: je Gerät die Einlieferungen im
  gewählten Zeitraum (heute / 7 / 30 / 90 Tage) mit TLS, Klartext, intern,
  extern, abgelehnt — dazu Geräteverwaltung, Abweisungen und Lernmodus.
  Das Mail-Protokoll hält dafür je Einlieferung fest, ob sie verschlüsselt kam
  und ob ein Ziel ausserhalb lag.
- Vier Seiten: Dashboard, Einrichtung, Einstellungen, Protokolle.

## 0.1.0 — 2026-09-04

Erste Fassung: das SMTP-Relay des EXO Signature Gateway als eigenständiger,
schlanker Dienst.

- **Mailpfad**: Geräteliste, Lernmodus, Absender- und Zielgrenze sind mit dem
  Gateway inhaltsgleich (`smtp_relay.py`, `relay_hosts.py`; geprüft von
  `tools/driftcheck.py`). Der Rückweg geht über den Smarthost auf Port 25 oder
  wahlweise über Port 587 mit Dienstkonto. Gezählt wird zugestellte Post;
  scheitert der Smarthost, antwortet der Dienst mit 451.
- **Adressquelle**: Postfachliste per ExchangeOnlineManagement-Modul, mit
  Plattencache über Neustarts hinweg, ergänzt um Adressen von Hand.
- **Weboberfläche**: Übersicht, Geräte, Einstellungen, Protokolle. Nur örtliche
  Anmeldung, gedrosselt; Herkunftsprüfung und Sicherheits-Header.
- **Zertifikate**: TLS-Zertifikat selbstsigniert oder aus PFX; Auth-Zertifikat
  für die App-Registrierung ohne `openssl`-Binary.
- **Exchange Online**: Anmeldetest und Inbound-Connector (Zertifikat- oder
  Adressvariante) über `scripts/setup_relay_connector.ps1`, PowerShell 5.1 und 7.
- **Verpackung**: Docker (amd64/arm64), systemd-Unit, Windows-Dienst mit
  `windows/install.ps1`.
