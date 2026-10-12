# Changelog — EXO SMTP Relay

## 0.2.10 — 2026-10-13 — Dashboard und Geräteseite: nichts mehr abgeschnitten

- **Gerätetabellen liefen rechts über.** Die Tabellen auf Dashboard und
  Geräteseite brauchen 820–1150px, die Seite war auf 960px begrenzt. Die letzte
  Spalte („Extern erlaubt“ bzw. die Namensfelder) stand deshalb abgeschnitten im
  waagerechten Rollbereich, unabhängig von der Fenstergrösse. Beide Seiten
  dürfen jetzt ab 1100px Fensterbreite bis 1600px breit werden, wie die breiten
  Seiten des Gateways. Darunter bleibt der Rollbereich.
- **Kachel „davon mit TLS“ brach um.** Der Wert „25 (78 %)“ stand in der grossen
  Kennzahlschrift und brach in einer Sechstel-Kachel dreizeilig um. Der Anteil
  steht jetzt in der Beschriftung („davon mit TLS (78 %)“), die Kennzahl allein
  darüber.
- Geprüft wird beides bei 1280px und 393px im Browser: jede Kennzahl einzeilig,
  keine Tabelle breiter als ihr Platz, keine Seite breiter als das Fenster.

## 0.2.9 — 2026-10-11 — Alte Geräte bekommen wirklich TLS 1.0/1.1 und statisches RSA

- **STARTTLS scheiterte bei Altgeräten am Handshake.** Der Listener setzte nur
  die Mindestversion auf TLS 1.0. Unter OpenSSL 3 genügt das nicht: Die
  Sicherheitsstufe 2 der Vorgabe verbietet TLS 1.0/1.1 samt deren
  SHA-1-Signaturen, und mit Pythons Cipherliste blieb unter OpenSSL 3.5 — so im
  ausgelieferten Abbild — keine Suite mit statischem RSA-Schlüsseltausch übrig.
  Gemessen im Abbild (Python 3.11.15, OpenSSL 3.5.6): Ein Client mit nur
  `TLS_RSA_WITH_AES_128_CBC_SHA256` (typisch für ältere HP-Multifunktionsgeräte)
  bekam keinen gemeinsamen Cipher, TLS 1.0 und 1.1 wurden als Protokoll
  abgelehnt. Unter OpenSSL 3.0 ging die RSA-Suite, TLS 1.0/1.1 nicht. Ein Gerät,
  das STARTTLS versucht, weicht danach in der Regel nicht selbst auf Klartext aus
  — es konnte schlicht nicht einliefern.
- **Jetzt:** Die Geräteseite nutzt OpenSSLs `DEFAULT`-Liste auf Stufe 0. Damit
  gehen TLS 1.0, 1.1 und die statischen RSA-Suites wieder. Die Reihenfolge
  bestimmt der Server: Ein moderner Client bekommt weiterhin TLS 1.3, ein Client
  mit TLS 1.2 eine ECDHE-Suite mit AES-GCM; die alten Suites greifen nur, wenn
  das Gerät nichts anderes kann. Unverschlüsselte, anonyme, RC4- und DES-Suites
  sind nicht enthalten.
- Nur die **Geräteseite** ist gelockert. Der Weg zu Exchange und die
  Weboberfläche bleiben bei den strengen Vorgaben.
- Statisches RSA setzt ein RSA-Zertifikat voraus. Das selbstsignierte Zertifikat
  des Relays ist eines; mit einem importierten EC-Zertifikat stehen nur die
  ECDHE-Suites zur Verfügung.
- Neue Tests machen einen echten Handshake gegen den Kontext des Dienstes (TLS
  1.0, 1.1, nur `AES128-SHA256`, nur `AES128-SHA` unter TLS 1.0) und prüfen die
  Gegenrichtung: Forward Secrecy für moderne Clients, keine schwachen Suites,
  keine Lockerung auf der Exchange-Seite.

**Zu tun:** Aktualisiere, wenn Geräte mit TLS 1.0/1.1 oder nur statischem RSA
einliefern sollen. Sonst ändert sich nichts.

## 0.2.8 — 2026-10-07 — App nur noch mit Leserechten, Schlüssel erneuert sich selbst

- **Exchange-Administrator nur noch für den Connector.** Bisher behielt die App
  des Relays die Entra-Rolle *Exchange-Administrator* dauerhaft — wer die
  `auth.pfx` kopierte, konnte sich damit von überall als Exchange-Administrator
  anmelden. Jetzt stuft der Assistent sie nach dem Anlegen des Connectors herab:
  Sie kommt in eine Exchange-Rollengruppe *„<Name> - nur lesen“* (*View-Only
  Recipients* + *View-Only Configuration*), danach wird die Entra-Rolle entfernt
  — in dieser Reihenfolge, damit die App nie ganz ohne Recht dasteht. Für spätere
  Änderungen am Connector holst du das Admin-Recht unter *Einrichtung → Rechte der
  App* kurz zurück.
- **Exchange übernimmt das Herabstufen verzögert.** Die Entra-Rolle ist sofort
  weg, Exchange liess die App aber noch über drei Stunden schreiben (in einer
  stündlichen Messung: nach 202 Minuten noch Admin, nach 263 Minuten Leser); eine
  Frist nennt Microsoft nicht. Der Dienst misst deshalb nach dem Herabstufen stündlich
  nach und zeigt unter *Rechte der App*, ob Exchange es übernommen hat und nach
  wie vielen Minuten. *Rechte jetzt messen* prüft den Stand sofort.
- **Schlüssel der App: ein Jahr statt zehn, Erneuerung von selbst.** 30 Tage vor
  Ablauf trägt der Dienst ein neues Schlüsselpaar in Entra ein (Graph `addKey`),
  stellt erst danach lokal um und trägt den alten Schlüssel im nächsten
  stündlichen Lauf aus (`removeKey`; Entra kennt den neuen Schlüssel in den ersten
  Minuten noch nicht überall). Dafür braucht die App kein zusätzliches Recht.
  Scheitert das Eintragen, bleibt der alte Schlüssel in Gebrauch; gelingt das
  Austragen einen Tag lang nicht, steht es unter *Schlüssel der App*.
- Beides gilt nur für eine App, die der Assistent selbst angelegt hat. Nutzt das
  Relay über ein importiertes Zertifikat die App des Signatur-Gateways mit, fasst
  es deren Rechte und Schlüssel nicht an — das Gateway braucht Schreibrechte.
- **Betrieb ganz ohne App-Registrierung** ist im README beschrieben: Postfach-
  adressen bzw. `@domäne`-Einträge von Hand, Connector per PowerShell.

**Zu tun bei einer bestehenden Installation:** Melde dich unter *Einrichtung*
in Schritt 3 einmal neu an. Das ersetzt den Zehn-Jahres-Schlüssel durch einen
mit einem Jahr Laufzeit und kennzeichnet die App als eigene. Danach unter
*Rechte der App* auf *Jetzt auf Lesen herabstufen*. Ohne diese Schritte bleibt
alles wie bisher.

## 0.2.7 — 2026-10-07 — Domäneneinträge in der Handliste

- Unter *Adressen von Hand* gilt ein Eintrag wie `@firma.de` jetzt als ganze
  Domäne: Jede Adresse dieser Domäne zählt als internes Ziel. Wer das Relay ohne
  App-Registrierung betreibt, braucht damit eine Zeile je Domäne statt jeder
  einzelnen Adresse samt Aliasen. Die Grenze ist etwas gröber — auch Adressen, die
  es nicht gibt, gelten dann als intern; Exchange weist sie selbst ab. Sie gilt
  nur, wo sie ausdrücklich eingetragen ist; die abgefragte Postfachliste bleibt
  adressgenau.

## 0.2.6 — 2026-10-06 — Scanner-Abbrüche wirklich leiser

- Abgebrochene TLS-Handshakes fremder Rechner (meist Port-Scanner) erscheinen jetzt
  wie vorgesehen als einzeilige INFO-Meldung statt als ERROR mit Traceback. Der
  Filter griff bisher nie, weil der SMTP-Baustein solche Abbrüche in einer eigenen
  Hülle meldet und nur die Hülle geprüft wurde. Echte Fehler bleiben ERROR.
  Gemeinsamer Baustein mit dem Gateway; Test jetzt auch hier, mit echtem Verkehr.

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
