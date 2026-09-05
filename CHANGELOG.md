# Changelog — EXO SMTP Relay

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
