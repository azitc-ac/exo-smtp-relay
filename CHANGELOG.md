# Changelog — EXO SMTP Relay

## 0.2.44-beta — 2026-11-24 — Korrekturen aus dem Labtest von 0.2.43

- **Windows: `install.ps1` meldete nach einem Upgrade weiter „admin / admin -
  bitte aendern“**, obwohl das Kennwort längst geändert war. Der Installer fragt
  jetzt dieselbe Regel wie die Oberfläche (neues Modul `kennwort_stand.py`,
  `ADMIN_PASSWORD_HASH` in settings.json): mit eigenem Kennwort „Kennwort
  unveraendert“, sonst weiter der Hinweis. Beim Upgrade heißt es außerdem
  „Aktualisiert.“ statt „Fertig.“ und „Dienst aktualisiert (Starttyp
  unveraendert)“ statt „(Autostart)“ — ein Upgrade ändert den Starttyp nicht.
- **Windows: `data\logs\service.log` in UTF-8.** Die Datei nimmt stdout/stderr
  des Anwendungsprozesses auf; ohne Konsole schrieb Python sie in der
  ANSI-Codepage („�bersprungen“). Der Dienst-Wrapper setzt jetzt
  `PYTHONIOENCODING=utf-8` für den Kindprozess. `app.log` war schon UTF-8.
  Nebenbei schließt der Wrapper seinen Handle auf service.log nach dem Start
  des Kindes (vorher blieb je Neustart einer offen).
- **Windows: Upgrade per `install.ps1` verliert nichts mehr.** Bisher schrieb
  der Installer die Dienst-Umgebung (Registry, `Environment`) jedesmal neu:
  eigene Einträge wie `WEBUI_PASSWORD` gingen verloren, und ohne `-WebPort`
  sprang ein eigener Port auf 8443 zurück. Jetzt führt `windows/dienst_umgebung.py`
  zusammen: gesetzt werden nur `PYTHONPATH` und `WEBUI_PORT`, alles andere
  bleibt. Ohne `-WebPort` bleibt der bisherige Port, 8443 gilt nur bei der
  Erstinstallation. Wichtig, weil bestehende Windows-Installationen ohne
  nachgeladene Wurzelzertifikate für 0.2.43+ einmal `install.ps1` ausführen müssen.
- **Windows: Upgrade öffnet die Firewall nicht mehr weiter als vorher**
  (sicherheitsrelevant). Bisher löschte `install.ps1` die Relay-Regeln und legte
  sie mit allen Profilen und ohne Quelladressen neu an; eigene Einschränkungen
  (RemoteAddress, Profile, Enabled) gingen verloren. Jetzt bleiben bestehende
  Regeln stehen, angepasst wird nur der Port. Nach einem Portwechsel per
  `-WebPort` wird die bisherige Web-Regel umgestellt, sodass genau eine
  übrig bleibt. Die Erstinstallation legt die Regeln wie bisher an.

**Im Labtest prüfen:** `install.ps1` setzt beim Upgrade weiter die Rechte auf
`data\` (Vererbung aus, SYSTEM und Administratoren). Ob dabei eigene, ausdrücklich
gesetzte Einträge verloren gehen, ist nicht geprüft: `Get-Acl` auf `data\` vor und
nach dem Upgrade vergleichen (siehe docs/release-test.md).

**Zu tun:** nichts. Die UTF-8-Ausgabe greift nach einem Knopf-Update erst mit
dem nächsten Neustart des Dienstes (der laufende Wrapper bleibt bis dahin der
alte); ältere Zeilen in service.log bleiben in der alten Codierung.

## 0.2.43-beta — 2026-11-24 — Korrekturen aus dem Labtest von 0.2.42

- **Docker: Start mit root-eigenem `./data`** (Blocker). Fehlte `./data`, legte
  Docker es als root an; das Abbild lief als `appuser` und brach mit
  `PermissionError: '/app/data/logs'` in einer Neustartschleife ab. Neu:
  `docker-entrypoint.sh` startet als root, legt `/app/data` an, setzt den
  Besitz NUR dort, wo er nicht schon `appuser` gehört, und wechselt per
  `setpriv` auf `appuser` (uid jetzt fest 1000). Der Bind-Mount `./data` bleibt
  — bestehende Installationen ändern sich nicht. Die CI startet das Abbild mit
  frischem, root-eigenem Bind-Mount und wartet auf „healthy“.
- **Windows: „Aktualisierung prüfen“ auf frischem Server** scheiterte mit
  `CERTIFICATE_VERIFY_FAILED` — Windows lädt Wurzelzertifikate erst bei Bedarf
  nach. Neues Modul `tls_vertrauen.client_kontext()`: Systemspeicher UND
  certifi. Ein Pfad für alle ausgehenden Verbindungen: GitHub-API und
  Downloads (`aktualisierung.py`), STARTTLS zu Exchange Online bzw. Smarthost
  (`smarthost.py`), Graph und Anmeldung über httpx (`setup_wizard.py`,
  `auth_cert.py`, `pkce.py`). Eine Firmen-CA aus dem Systemspeicher gilt weiter.
  Ein Test erzwingt, dass keine neue Stelle daran vorbeigeht.
- **Selbstsigniertes Zertifikat passt zum Startmenü-Link:** Es trägt jetzt
  neben dem eingetragenen Hostnamen FQDN und kurzen Rechnernamen als SAN
  (vorher ohne Hostnamen nur `exo-smtp-relay.local`). Im Container bleibt es
  bei `exo-smtp-relay.local`. Bestehende Zertifikate bleiben, bis du sie unter
  *Einstellungen → TLS-Zertifikat des Listeners* neu erzeugst.
- README: Abschnitt Docker (Daten und Rechte) und systemd (Port 8080, Wechsel
  per Drop-in) ergänzt. Kein Portwechsel für bestehende Installationen.

**Zu tun:** nichts. Windows: Wer den Link ohne Zertifikatswarnung will, erzeugt
das selbstsignierte Zertifikat einmal neu (*Einstellungen → TLS-Zertifikat des Listeners*).
## 0.2.42 — 2026-11-24 — Release-Schlüssel eingebaut

- `app/update_paket.py`: zwei öffentliche Prüfschlüssel in `RELEASE_SCHLUESSEL`.
  Der Hauptschlüssel signiert die Releases (GitHub-Secret `RELEASE_SIGNING_KEY`),
  der Reserveschlüssel liegt offline und hält Releases möglich, falls der
  Hauptschlüssel verloren geht.
- Test `test_signieren_und_pruefen_rund`: Die CI-Gegenprobe erwartet jetzt
  immer eine Ablehnung, weil der Testschlüssel nie eingebaut ist. Der Test zum
  Phasenablauf ersetzt die eingebauten Schlüssel unabhängig davon, ob das
  Tupel leer oder mehrzeilig ist.

**Zu tun:** nichts.
## 0.2.41 — 2026-11-24 — Testkorrektur unter Windows

- `tests/test_aktualisierung.py` liest und schreibt Dateien ausdrücklich als
  UTF-8. Unter Windows las `read_text()` das Dockerfile mit cp1252 und brach
  an einem Umlaut ab (CI „Tests unter Windows“ zu 0.2.40 rot). Kein Einfluss
  auf den Dienst.

**Zu tun:** nichts.
## 0.2.40 — 2026-11-24 — Aktualisierung aus fertigen Releases

- **Release-Workflow** (`.github/workflows/release.yml`), ausgelöst NUR durch
  ein Tag `v*`: Windows-ZIP und Linux-Archiv (beide mit `LICENSE.md` und
  `NOTICE`), `SHA256SUMS`, Ed25519-Signatur `SHA256SUMS.sig`, Abbild
  `ghcr.io/azitc-ac/exo-smtp-relay` für amd64 und arm64 (nativ gebaut) mit den
  Tags `<version>` und `stable` bzw. `beta` (`-beta[.N]` im Tag). Ohne Secret
  `RELEASE_SIGNING_KEY` oder mit einem Schlüssel, der nicht zum eingebauten
  passt, bricht der Lauf vor jeder Veröffentlichung ab.
- **Neue Seite Aktualisierung** mit Band auf jeder Seite, wenn eine neuere
  Fassung im Kanal vorliegt. Einstellungen `UPDATE_KANAL` (stable/beta) und
  `UPDATE_PRUEF_STUNDEN` (0/6/24/168, Vorgabe 24). Geprüft wird über die
  GitHub-Releases-API; eingespielt nur auf Knopfdruck.
- **Einspielen** je Betriebsart: Windows-Dienst über den Dienst-Wrapper,
  Linux über die neue Unit `exo-smtp-relay-update.path/.service` (root),
  beide mit zweiter Prüfung von Prüfsumme und Signatur, Austausch von `app/`
  und Begleitdateien, Neustart, Rückfall, wenn die neue Fassung nicht stabil
  hochkommt (`update_tausch.py`). `data/` bleibt unberührt, die venv wird nur
  bei geänderter `requirements.lock` angepasst. Docker: Anzeige von
  `docker compose pull && docker compose up -d` bzw. Hinweis auf Watchtower.
- Ohne eingebauten Prüfschlüssel (`RELEASE_SCHLUESSEL` in
  `app/update_paket.py`, noch leer) wird nichts eingespielt.
- Backups lassen `data/update/` (geladene Pakete) aus.
- systemd-Unit: Einrichtung aus dem Release-Archiv, `RELAY_BETRIEBSART=systemd`.

**Zu tun:** Windows: einmal `windows\install.ps1` aus diesem Paket ausführen
(neuer Dienst-Wrapper). Linux: `exo-smtp-relay-update.path` einrichten (Kopf
von `linux/exo-smtp-relay-update.service`). Docker: nichts.
## 0.2.39 — 2026-11-24 — Lizenz: Apache License 2.0

- Das Relay steht ab dieser Fassung unter der Apache License 2.0 und ist damit
  Open Source. Bisher galt die PolyForm Internal Use License: kostenlos, aber
  Weitergeben und der Betrieb als Dienst für Dritte waren untersagt. Beides ist
  jetzt erlaubt; Bedingungen sind Lizenztext und `NOTICE` beizulegen und
  Änderungen kenntlich zu machen. Die Lizenz enthält eine Patentlizenz und
  überträgt keine Rechte am Produktnamen.
- Frühere Fassungen bleiben unter der Lizenz, unter der sie erschienen sind.
- Container-Abbild und Windows-Installation enthalten jetzt `LICENSE.md` und
  `NOTICE` (Apache-2.0, Abschnitt 4).

**Zu tun:** nichts.
## 0.2.38 — 2026-11-24 — TLS-Zertifikat von Let's Encrypt (wie Gateway)

- **Einstellungen → TLS-Zertifikat → Let's Encrypt**, zwei Wege wie im Gateway:
  - **HTTP-01 über certbot** (`le_certbot.py`) mit **automatischer
    Erneuerung** im Zeitplaner (stündlich geprüft, höchstens ein Versuch je
    Tag, ab `LE_RENEW_DAYS` vor Ablauf). Ausstellen und Erneuern nutzen
    dieselben Verzeichnisse unter `data/` und dieselbe Übernahme. Port 80
    öffnet das Relay nur während des certbot-Laufs und liefert dort nur
    `/.well-known/acme-challenge/`. certbot steckt im Docker-Abbild; unter
    Linux ohne Docker `apt install certbot`; **unter Windows gibt es certbot
    nicht mehr** — dort DNS-01 oder PFX.
  - **DNS-01 von Hand** (`tls_acme_dns.py`, alle Plattformen, kein offener
    Port): TXT-Record anzeigen, nach dem Setzen ausstellen. Erneuerung von
    Hand.
- Schlüssel immer **RSA 2048** (Gateway: certbot-Vorgabe ECDSA bzw. EC):
  Altgeräte mit nur statischem RSA-Schlüsseltausch bekämen sonst keinen
  Handshake.
- Das neue Zertifikat **nutzt der Listener sofort** (0.2.37), der Rückweg zu
  Exchange ab dem nächsten Versand — ohne Neustart.
- **Connector am Zertifikat:** Ein Zertifikat von Let's Encrypt gilt als
  CA-signiert, die Sperre aus 0.2.33 greift nicht mehr. Eines aus der
  Testumgebung (Staging) wird abgelehnt.
- Eine Erneuerung überschreibt **kein später importiertes Zertifikat**
  (Abgleich mit certbots Zertifikat — fehlt im Gateway).
- Neues **Band auf jeder Seite**, wenn das TLS-Zertifikat binnen
  `LE_RENEW_DAYS` abläuft — die Erinnerung für DNS-01 und PFX.
- **Backup:** enthält die offene DNS-01-Bestellung, nicht certbots
  Verzeichnisse (Symlinks, wie Gateway). Nach einer Wiederherstellung stellt
  der Zeitplaner bei fälligem Ablauf über HTTP-01 neu aus.
- Neue Einstellungen mit Oberflächenpunkt: `LE_DOMAIN`, `LE_EMAIL`,
  `LE_AUTO_RENEW`, `LE_RENEW_DAYS`, `ACME_HTTP_PROXY`; Zustand `LE_LAUF`.
  `acme_client.py` ist eine Spiegeldatei des Gateways (driftcheck).

**Zu tun:** Für HTTP-01 Port 80 aus dem Internet zum Relay freigeben
(`docker-compose.yml` bildet ihn jetzt ab). Ohne Let's Encrypt nichts.

## 0.2.37 — 2026-11-24 — Neues TLS-Zertifikat wirkt ohne Neustart

- Der SMTP-Listener baute seinen TLS-Kontext nur beim Start. Ein neues
  Zertifikat (selbstsigniert neu, PFX-Import, Wiederherstellung) bot er erst
  nach einem Neustart des Dienstes an. Jetzt prüft er vor jeder neuen
  Verbindung, ob sich Zertifikat oder Schlüssel auf der Platte geändert
  haben, und lädt sie dann neu. Passt das Paar (noch) nicht zusammen, bleibt
  das bisherige — STARTTLS fällt nie aus. Voraussetzung für die automatische
  Erneuerung mit Let's Encrypt (nächster Schritt).
- Der Rückweg zu Exchange las das Zertifikat schon bisher bei jedem Versand.
  Nur die Weboberfläche braucht für ein neues Zertifikat weiter einen
  Neustart; die Hinweise sagen das jetzt so.

**Zu tun:** nichts.

## 0.2.36 — 2026-11-24 — Backup: Datenbanken auch bei laufendem Dienst unter Windows

- Die Wiederherstellung tauschte `relay_hosts.db` und `mail_audit.db` als
  Datei aus. Unter Windows scheitert das, solange der Dienst eine Verbindung
  offen hat (in der CI gemessen: WinError 5/32); unter Linux schrieben offene
  Verbindungen in die alte Datei weiter. Jetzt ersetzt SQLite den Inhalt der
  bestehenden Datenbank (Backup-Schnittstelle), Rechte und ACL bleiben.

**Zu tun:** nichts.

## 0.2.35 — 2026-11-24 — Backup und Wiederherstellung, optional mit Passwort

- Neue Seite **Backup** (wie Gateway f0c95af, gleiche Routen
  `/api/backup/*`): Das Archiv enthält alles für einen Umzug —
  Einstellungen samt Rechte-Stand der App, Geräteliste
  (`relay_hosts.db`), TLS-Zertifikat mit Schlüssel, Anmeldezertifikat bei
  Exchange Online (`auth.pfx`), Mail-Protokoll und den letzten Stand der
  Postfachliste. Logs und `settings.bak` bleiben draußen.
- **Mit Passwort** verschlüsselt (AES-256-GCM, Schlüssel per scrypt), Datei
  `.relaybak`. **Ohne Passwort** ein ZIP mit allen Geheimnissen im Klartext —
  die Seite warnt deutlich und fragt vor dem Herunterladen nach, das Archiv
  selbst trägt den Hinweis in `README.txt`.
- **Wiederherstellen** zeigt zuerst den Inhalt, dann wählst du aus. Jede
  Datei wird mit Rechten 600 (Verzeichnis 700) geschrieben, unter Windows
  mit der ACL von `data\`. Das TLS-Zertifikat geht an den Pfad, den das
  neue System verwendet — auch wenn er dort per `SMTP_TLS_CERT` abweicht.
  Datenbanken werden über SQLite gelesen, nicht als Datei kopiert.
- Ein Backup des Signature Gateway wird abgelehnt (eigenes Kennzeichen
  `relay-backup.json`).
- Die Einrichtung weist in Schritt 1 auf den Umzugsweg hin.

**Zu tun:** nichts. Für einen Umzug: Backup auf dem alten Relay ziehen,
auf dem neuen Schritt 1 der Einrichtung abschließen, Backup einspielen,
Dienst neu starten. Danach gilt das Kennwort des alten Relays.

## 0.2.34 — 2026-11-24 — Betriebsbänder auf jeder Seite

- Wie im Gateway (5bd5b1b): Betriebszustände stehen als Band oben auf jeder
  Seite statt nur in der Übersicht — keine Seite kann sie vergessen
  (Jinja-Globals in `deps.py`, Ausgabe in `base.html`):
  - **Relay abgeschaltet** (gelb) — jede Einlieferung wird abgewiesen. Das
    Gegenstück zum Wartungsmodus des Gateways.
  - **Keine Postfachadressen bekannt** (rot) — jede Einlieferung bekommt 451.
  - **Zustellfehler** (rot) — Einlieferungen, die Exchange Online seit der
    letzten erfolgreichen Zustellung nicht angenommen hat (höchstens 24
    Stunden zurück). Klappt die nächste Zustellung, verschwindet das Band.
- Beide ersten Bänder erst nach abgeschlossener Einrichtung. Die Hinweise
  darauf in der Übersicht und auf der Geräteseite entfallen.
- Die Anmeldeseite zeigt keinen Betriebszustand mehr; bisher stand dort das
  Band des Lernmodus.

**Zu tun:** nichts.

## 0.2.33 — 2026-11-24 — Connector am Zertifikat nur mit CA-Zertifikat

- Der Modulkopf von `tls_cert.py` behauptete, ein selbstsigniertes
  Zertifikat genüge für einen Inbound-Connector, der das Relay am Zertifikat
  erkennt (`TlsSenderCertificateName`). Das widersprach dem Connector-Skript,
  der Einrichtung und der README und der Microsoft-Dokumentation, die dafür
  ein CA-signiertes Zertifikat verlangt, dessen Subject oder SAN eine
  akzeptierte Domäne enthält. Der Text ist berichtigt und nennt die Quelle.
- Mit selbstsigniertem Zertifikat und ohne öffentliche IP legt das Relay den
  Connector nicht mehr an, weder über die Einrichtung noch über die
  Einstellungen, sondern sagt, was fehlt. Bisher entstand ein Connector, der
  das Relay nicht erkennt.

**Zu tun:** nichts. Ein bestehender Connector bleibt unverändert.

## 0.2.32 — 2026-11-24 — SMTP-Antworten nur in ASCII

- Wie im Gateway (v1.12.24): Antwortzeilen des SMTP-Listeners gehen nur
  noch in ASCII hinaus (RFC 5321). Umlaute werden umgeschrieben
  (`Absenderdomaene fuer …`), andere Zeichen durch `?` ersetzt. Bisher kamen
  die Ablehnungen des Relays („Absenderdomäne … nicht zulässig“, „Relay
  temporär nicht verfügbar“ u. a.) als UTF-8 an; manche Geräte und Outlook
  zeigen das verstümmelt an.

**Zu tun:** nichts.

## 0.2.31 — 2026-11-24 — Postfachliste: „abgelaufen“ auch kurz nach dem Systemstart

- Wie im Gateway (v1.12.68): Der Zeitstempel der Postfachliste steht für
  „abgelaufen“ jetzt auf minus unendlich statt 0. Die Zeitbasis zählt ab
  Systemstart; in der ersten Stunde nach einem Neustart galt 0 noch als
  frisch. Dann wirkte „neu abfragen“ (z. B. nach dem Ändern der
  Handeinträge) nicht, und ein von der Platte geladener Stand wurde nicht
  sofort nachgeladen.

**Zu tun:** nichts.

## 0.2.30 — 2026-11-24 — Protokollstufe wirkt sofort

- Wie im Gateway (v1.11.55): Die Protokollstufe aus Einstellungen → Betrieb
  gilt jetzt für das ganze Relay, beim Start und sofort beim Speichern
  (`log_manager.stufe_anwenden`). Bisher setzte der Start die Stufe nur aus
  der Umgebungsvariable `LOG_LEVEL`; die Einstellung erreichte nur den
  Webserver und erst nach einem Neustart.

**Zu tun:** nichts.

## 0.2.29 — 2026-11-24 — Keine Seite in einen Suchindex

- Wie im Gateway (v1.9.172): Jede Antwort der Weboberfläche trägt
  `X-Robots-Tag: noindex, nofollow`. Als Kopfzeile, nicht als
  `robots.txt`-Sperre: Eine gesperrte Adresse ruft ein Suchdienst nicht mehr
  ab und sieht das `noindex` nie.

**Zu tun:** nichts.

## 0.2.28 — 2026-11-24 — Lücken-Pakete angehoben, pip-audit/bandit/ruff in der CI

- Wie im Gateway (v1.11.6): `cryptography` 50.0.0, `certifi` 2026.10.18,
  `PyJWT` 2.15.0, `urllib3` 2.8.0. `pip-audit` meldete gegen den alten Stand
  22 bekannte Lücken in diesen vier Paketen, gegen den neuen keine.
- Die CI prüft jetzt zusätzlich mit `pip-audit` (Lock-Datei), `bandit`
  (hohe Schwere und Sicherheit) und `ruff` (Fehlerklassen F ohne
  F401/F811), dieselben Fassungen und Schalter wie im Gateway. Sie läuft
  ausserdem wöchentlich, weil neue Lücken ohne Codeänderung entstehen.
  bandit und ruff finden im Relay derzeit nichts.

**Zu tun:** Relay aktualisieren (neue Paketstände im Container bzw. in der
Windows-Installation).

## 0.2.27 — 2026-11-24 — PowerShell-Werte nur als Literal

- Wie im Gateway (v1.10.117): Neuer Baustein `ps_text.py` (1:1 vom
  Gateway). Jeder Wert, der in ein PowerShell-Skript für Exchange Online
  eingesetzt wird (Relay-Name, App-ID, Organisation, Zertifikatspfad,
  Rollen- und Cmdlet-Listen), geht durch `ps_text.lit`/`array` und steht in
  einfachen Anführungszeichen. Bisher standen Relay-Name, App-ID,
  Organisation und Zertifikatspfad in `exo_setup.py` und `exo_mailboxes.py`
  ungeschützt im Skript; ein Apostroph im Relay-Namen brach die
  Connector-Prüfung.
- `tests/test_ps_text.py` prüft alle Skript-f-Strings im Code.

**Zu tun:** nichts.

## 0.2.26 — 2026-11-24 — Sitzungen enden beim Kennwortwechsel

- Wie im Gateway (v1.11.3): Jedes Sitzungsmerkmal trägt eine Kennung des
  gerade gültigen Kennworts. Ein Kennwortwechsel, auch über `WEBUI_PASSWORD`
  in der Umgebung, beendet alle älteren Sitzungen sofort. Bisher galt eine
  Sitzung ihre vollen 8 Stunden weiter. Wer das Kennwort ändert, bleibt selbst
  angemeldet (neues Merkmal mit der Antwort).

**Zu tun:** nichts. Nach dem Update muss sich jeder einmal neu anmelden, weil
ältere Merkmale noch keine Kennung tragen.

## 0.2.25 — 2026-11-24 — Mit dem Auslieferungs-Kennwort nur Kennwort ändern

- Wie im Gateway (v1.10.113): Wer mit dem Auslieferungs-Kennwort (`admin`,
  auch `changeme` oder leer) angemeldet ist, erreicht nur noch die
  Einrichtung (Schritt 1) und das Ändern des Kennworts. Andere Seiten leiten
  dorthin, API-Aufrufe bekommen 403. Bisher wies nur das Kopfbanner darauf
  hin, gesperrt war nichts. Das gilt für Sitzung und HTTP-Basic gleichermassen
  und auch für den Rückruf der Microsoft-Anmeldung.
- Ein eigenes Kennwort über `WEBUI_PASSWORD` in der Umgebung zählt jetzt als
  geändert; das Banner „Vorgabepasswort aktiv“ erscheint dann nicht mehr.

**Zu tun:** Läuft ein Relay noch mit `admin`, nach dem Update einmal das
Kennwort in der Einrichtung (Schritt 1) setzen.

## 0.2.24 — 2026-11-24 — Ziel nach der Anmeldung geprüft

- Wie im Gateway (v1.9.138): Das Ziel nach der Anmeldung (`next`) wird an
  EINER Stelle geprüft (`anmeldung._sicheres_ziel`). Erlaubt sind nur Pfade
  auf diesem Server; `//fremd.example`, `/\fremd.example`, `javascript:` und
  Steuerzeichen führen auf die Startseite. Bisher setzte die Anmeldeseite das
  Ziel ungeprüft aus der Adresszeile in `location.href`, und eine schon
  angemeldete Sitzung wurde ungeprüft weitergeleitet.

**Zu tun:** nichts.

## 0.2.23 — 2026-11-21 — Gemeinsame Frontend-Helfer vom Gateway

- `common.js` vom Gateway übernommen (v1.12.144): Ein langer Hinweis, dessen
  Text erst nach dem Seitenaufbau gesetzt wird, bekommt bei Überlauf jetzt
  seinen Schalter „mehr“, statt ohne ihn gekürzt zu werden.

**Zu tun:** nichts.

## 0.2.22 — 2026-11-20 — Gemeinsame Stylesheets vom Gateway

- `style.css` und `dark-mode.css` vom Gateway übernommen (v1.12.120). Entfernt
  sind drei Stilregeln früherer Vorschlagslisten, die weder Gateway noch Relay
  noch benutzen. An der Oberfläche ändert sich nichts.

**Zu tun:** nichts.

## 0.2.21 — 2026-11-16 — Gemeinsames Stylesheet vom Gateway

- `style.css` vom Gateway übernommen (v1.12.45). Knöpfe und Auswahllisten in
  der Feldspalte einer Einstellungszeile (`.settings-control`) nehmen ihre
  eigene Breite statt der ganzen Zeile. Das Relay verwendet diese Klasse
  derzeit nicht, an seiner Oberfläche ändert sich nichts.

**Zu tun:** nichts.

## 0.2.20 — 2026-11-16 — Gemeinsame Datei secure_io vom Gateway

- `secure_io.py` vom Gateway übernommen (v1.12.40). Verzeichnisse, die beim
  Anlegen einer geschützten Datei mit entstehen, bekommen jetzt 700 statt der
  Systemvorgabe; schon vorhandene bleiben unverändert. Die doppelt definierte
  `harden_file` ist auf die wirksame, nur einschränkende Fassung reduziert; am
  Verhalten ändert sich dadurch nichts.

**Zu tun:** nichts.

## 0.2.19 — 2026-11-11 — Abgebrochenes TLS: genauer Grund im Protokoll

- `smtp_rauschen.py` vom Gateway übernommen (v1.11.21). Bricht ein
  TLS-Aufbau an einem Zertifikat ab, nennt die Protokollzeile jetzt die
  Prüfmeldung, etwa „unable to get local issuer certificate“ (Wurzel
  unbekannt) oder „unsuitable certificate purpose“ (falscher
  Verwendungszweck). Bisher stand dort nur „SSLCertVerificationError“.

**Zu tun:** nichts.

## 0.2.18 — 2026-11-09 — Geräteliste: Spalte für die Rolle „vorgelagertes Gateway“

- `relay_hosts.py` und `smtp_relay.py` vom Gateway übernommen (v1.10.98). Die
  Geräteliste kennt dort die Rolle *vorgelagertes Gateway* für die
  Tenant-Weiterleitung zwischen zwei Gateways. Die Datenbank bekommt beim
  Start die Spalte `gateway` nachgetragen; das Relay wertet sie nicht aus,
  am Mailpfad ändert sich nichts.

**Zu tun:** nichts.

## 0.2.17 — 2026-11-09 — dark-mode.css: gesperrte mehrzeilige Felder

- `dark-mode.css` vom Gateway übernommen. Gesperrte mehrzeilige Felder
  (`textarea:disabled`) erscheinen im Dunkelmodus ausgegraut wie gesperrte
  Eingabefelder. Die Relay-Oberfläche hat derzeit kein solches Feld, sichtbar
  ändert sich nichts.

**Zu tun:** nichts.

## 0.2.16 — 2026-10-23 — common.js: Meldungen auch in ausgeblendeten Elementen

- `common.js` vom Gateway übernommen. `showMsg()` hebt jetzt auch das Attribut
  `hidden` auf. Bisher blieb eine Meldung in einer Fortschrittsanzeige nach
  `knopfAktion()` unsichtbar, denn dagegen setzt sich `[hidden] { display:
  none !important }` durch. Die Relay-Oberfläche nutzt diesen Ablauf nicht,
  sichtbar ändert sich nichts.

**Zu tun:** nichts.

## 0.2.15 — 2026-10-21 — Gemeinsame Helfer auf Gateway-Stand

- `common.js` vom Gateway übernommen. Ein gescheiterter Abruf (keine
  Verbindung, Antwort ohne JSON) ist jetzt von einer fachlichen Absage
  unterscheidbar (`abrufFehler`, `http`). `getJSON` wertet eine Antwort ohne
  JSON nicht mehr als leeren Erfolg. Neu ist `feldWert()`. Die vier Abrufe
  der Relay-Oberfläche gehen alle an Routen, die JSON liefern; ihr Verhalten
  bei Erfolg bleibt gleich.

**Zu tun:** nichts.

## 0.2.14 — 2026-10-21 — Stylesheet auf Gateway-Stand

- `style.css` und `dark-mode.css` vom Gateway übernommen. Neu sind die Regeln
  für die Aktionsmarken im Mailprotokoll des Gateways. Im Relay haben sie keine
  Wirkung, die Dateien bleiben aber deckungsgleich.

**Zu tun:** nichts.

## 0.2.13 — 2026-10-21 — Stylesheet auf Gateway-Stand

- `style.css` und `dark-mode.css` auf den Stand des Gateways gebracht. Neu sind
  die Regeln für Zustände, die Skripte setzen, statt selbst Farben zu setzen.
  Dazu gehört der Online-Punkt in der Kopfleiste, der jetzt auch im Relay
  darüber gefärbt wird.

**Zu tun:** nichts.

## 0.2.12 — 2026-10-21 — Gemeinsame Bausteine auf Gateway-Stand

- `common.js`, `style.css` und `dark-mode.css` auf den Stand des Gateways
  gebracht. Neu sind:
  - `knopfAktion()`: sperrt einen Knopf während einer Aktion, zeigt einen
    drehenden Pfeil und gibt ihn auch nach einem Fehler wieder frei;
  - `sendeFormular()` für Datei-Uploads;
  - `dateiHerunterladen()`;
  - die Stilregeln für Pfeil und Fortschrittsanzeige, im hellen und dunklen
    Modus.

  Die Seiten des Relays nutzen die neuen Funktionen noch nicht, ihr Verhalten
  ändert sich nicht.

**Zu tun:** nichts.

## 0.2.11 — 2026-10-21 — Anmeldefenster öffnet sich zuverlässig

- **Die Microsoft-Anmeldung in der Einrichtung konnte blockiert werden.** Das
  Fenster ging erst auf, nachdem das Relay die Anmeldeadresse geliefert hatte.
  Zu diesem Zeitpunkt gilt der Klick für den Browser nicht mehr als Klick des
  Nutzers, und der Browser blockierte das Fenster je nach Einstellung ohne
  Hinweis. Jetzt öffnet es sich im Klick selbst und kommt nach vorn. Blockiert
  der Browser es trotzdem, steht das in der Meldung.
- `common.js` auf den Stand des Gateways gebracht (`fensterImKlick()`,
  `anmeldeFenster()`). Ein Test lässt `window.open` nur noch dort zu.

**Zu tun:** nichts.

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
