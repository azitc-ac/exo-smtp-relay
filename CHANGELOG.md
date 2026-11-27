# Changelog — EXO SMTP Relay

## 0.2.60-beta — 2026-11-27 — Let's Encrypt im Einrichtungsassistenten, Aktualisierungsseite hängt nicht mehr

- **Einrichtung, Schritt 2 (Hostname): Let's Encrypt als empfohlener Weg.** Sobald der Hostname
  steht, stellt der Assistent das Zertifikat selbst aus: HTTP-01 (Port 80) oder DNS-01 mit
  DNS-Provider (z. B. IONOS), dann „ausstellen“. Es ist **derselbe Block** wie unter
  Einstellungen → TLS (neue Vorlage `_letsencrypt.html`, dieselben Felder, Endpunkte und dasselbe
  JavaScript), keine zweite Logik. Liegt ein Zertifikat von Let's Encrypt vor, steht dort
  „Ausgestellt von Let's Encrypt … gültig bis …“. Die Erkennung über die feste öffentliche IP ist
  sichtbar als Alternative genannt.
- **Schritt 4 (Connector)** sagt, welche Variante passt: mit Let's-Encrypt-Zertifikat das Feld für
  die IP leer lassen, ohne Zertifikat geht nur die IP-Variante, mit Verweis auf Schritt 2.
- **Aktualisierungsseite hing bei „Dienst startet neu …“** (Alex, Update 0.2.56 → 0.2.58-beta:
  Tausch fertig, Seite stand trotzdem still). Bis 0.2.59 zeigte JEDER gescheiterte Abruf diesen
  Text, ohne Zeitlimit: kein Netz, 401, eine Anmeldeseite statt JSON und 5xx. Jetzt nach dem Muster
  des Gateways: Der Abschluss wird über den **Versionsvergleich** erkannt (gemeldete Fassung ≠
  Fassung beim Laden der Seite), dann erscheint ein Dialog „Update erfolgreich! vX → vY“, „Neu
  laden“ lädt die Seite. 401 oder Anmeldeseite heißt: Dienst ist wieder da, die Seite lädt neu
  (bei abgelaufener Sitzung zur Anmeldung). Ist der Dienst 3 Minuten lang nicht erreichbar oder
  antwortet er weiter mit einem Fehler, steht die echte Meldung samt Pfad des Protokolls da, mit
  „Seite neu laden“. Browser-Tests spielen die Fälle durch. Hinweis: Das greift ab der ersten Seite,
  die von 0.2.60-beta ausgeliefert wurde. Ein Update, das noch von einer älteren Seite aus gestartet
  wird, läuft mit deren JavaScript.
- Geprüft: Sitzungen überleben einen Neustart (Geheimnis in `data/`, signiertes Merkmal). Daran lag
  der Hänger also nicht.
- **Log-Seite, gleiche Fehlerklasse:** Nach einem Neustart (oder nach einer Stunde) stand sie ewig
  auf „Verbinde…“, weil das Token für den Live-Stream nur im Speicher des alten Prozesses lag.
  Scheitert das Verbinden wiederholt, obwohl `/health` antwortet, lädt sie jetzt neu und holt sich
  so ein neues Token.

## 0.2.59-beta — 2026-11-27 — Pakete tragen das Commit-Datum statt 01.01.2020, Hinweis zur localhost-Fehlerseite

- **Zeitstempel der Dateien in den Release-Paketen:** Alle Einträge im Windows-ZIP, im
  Windows-Offline-ZIP und im Linux-Archiv tragen jetzt das **Commit-Datum der gebauten Fassung**
  statt des festen 01.01.2020. Der Windows-Explorer zeigt damit keine scheinbar veralteten
  Dateien mehr. Die Pakete bleiben reproduzierbar: zweimal gebaut ergibt dieselben Bytes.
- **Eine Quelle für den Zeitpunkt** (`update_paket.zeitstempel`): zuerst `SOURCE_DATE_EPOCH`,
  sonst `git log -1 --format=%ct` des gebauten Stands (klappt auch im flachen Checkout der CI).
  Den Ersatzwert 01.01.2020 gibt es nur noch für Testbäume ohne Git; `tools/release.py bauen`
  bricht ab, statt ihn für ein echtes Release zu nehmen. ZIP speichert Ortszeit ohne Zone im
  2-Sekunden-Raster ab 1980: der Wert wird in UTC geschrieben, auf gerade Sekunden abgerundet und
  frühestens auf 1980 gesetzt.
- **Einrichtung, erster Login:** Die Anleitung für die `localhost`-Fehlerseite steht jetzt in einem
  Warnkasten statt im Fließtext: „**Die Fehlerseite ist normal.** Kopiere die **komplette Adresse
  aus der Adressleiste** dieses Fensters und füge sie unten ins Feld ein.“ Derselbe Kasten (eine
  Vorlage) erscheint auch beim Login für Rechte hoch- oder herabstufen. Im Dark Mode lesbar, bei
  393 px ohne seitliches Scrollen (Browser-Test misst Kontrast und Breite in hell und dunkel).

## 0.2.58-beta — 2026-11-27 — „Über“-Fußzeile, Gleichlauf-Prüfung für den DNS-Provider-Katalog

- **Fußzeile auf jeder Seite** der Oberfläche (auch Anmeldung und Rückmeldeseite der Einrichtung):
  Logo, „Alexander Zarenko – IT Consulting“, Produkt und laufende Version, Links zu Produktseite,
  GitHub, Blog, Lizenz (Apache-2.0), Impressum und Datenschutz. Eine Vorlage
  (`templates/_fusszeile.html`), eingebunden über `base.html`; die Rückmeldeseite nutzt dieselbe
  Vorlage. Das Logo liegt unter `/static/azitc-logo.svg` und wird lokal ausgeliefert, das Relay
  lädt nichts von außen nach (auch offline vollständig). `style.css` bleibt unverändert, sie ist
  mit dem Gateway gespiegelt. Ein Test prüft Version, Links und Logo und geht dabei jede HTML-Route
  durch.
- **DNS-Provider-Katalog, Gleichlauf mit dem Gateway:** neues Register `app/dns_provider/FASSUNGEN`
  (Fassung plus Fingerabdruck des Manifests, Zeilen werden nur angehängt) und `LIESMICH.md` mit dem
  Ablauf für Änderungen. Neue Tests werden rot, wenn das Manifest ohne Versionssprung neu
  geschrieben wird, wenn eine Fassung zurückgeht oder wenn eine früher committete Fassung
  nachträglich einen anderen Inhalt bekommt (Abgleich mit der Git-Historie). Der Katalog selbst
  bleibt bei **1.0.0**, keine Datei des Katalogs wurde geändert.

## 0.2.57-beta — 2026-11-27 — Let's Encrypt DNS-01 über die DNS-API (IONOS; Azure DNS vorbereitet)

- **DNS-01 automatisch:** Mit einem DNS-Provider legt das Relay den TXT-Record
  `_acme-challenge.<name>` selbst an, wartet, bis er auf **jedem autoritativen Nameserver** steht
  (Zeitlimit `DNS01_WARTEN_S`, Vorgabe 300 s), löst die Prüfung aus und entfernt **immer** nur
  seinen eigenen Wert wieder, auch wenn etwas scheitert. Fremde TXT-Records unter demselben Namen
  (SPF, Nachweise, ein zweiter ACME-Wert) bleiben stehen. Mehrere Namen (Komma) und **Wildcards**
  (`*.example.com`) gehen nur auf diesem Weg.
- **Methodenschalter `LE_METHODE`** (`http01` | `dns01`) für Ausstellen UND Erneuern. Der Zeitplaner
  erneuert DNS-01-Zertifikate über die DNS-API, ohne certbot zu fragen. Ein importiertes bzw.
  fremdes Zertifikat überschreibt er nicht. Vorgabe unter Windows `dns01` (dort gibt es kein
  certbot), sonst `http01`. Wer über HTTP-01 bzw. DNS-01 ausstellt, setzt den Schalter mit.
- **Provider-Katalog `app/dns_provider/`** (Apache 2.0, Herkunft Relay, für den späteren Port ins
  Gateway): Basisklasse, `ionos.py`, `azure.py`, schlanke DNS-Abfrage (`dnsabfrage.py`, nur
  Standardbibliothek). `KATALOG_VERSION` 1.0.0 mit SHA-256 je Datei; ein Test gleicht Manifest und
  Inhalt ab und prüft, dass der Katalog keine Relay-Module importiert.
- **IONOS** (`api.hosting.ionos.com/dns/v1`, `X-API-Key: <Präfix>.<Geheimnis>`), gegen die offizielle
  OpenAPI-Beschreibung geprüft. **Azure DNS** (ARM, App-Registrierung oder verwaltete Identität) ist
  vorbereitet, aber nicht gegen ein echtes Azure erprobt.
- **Oberfläche:** Einstellungen → TLS-Zertifikat → Let's Encrypt: Methode, Provider, Felder je
  Provider (aus dem Katalog), „Verbindung testen“ (liest nur die Zonen), „Zertifikat über DNS-01
  ausstellen“ (mit Testumgebung). DNS-01 von Hand bleibt für „kein Provider“.
- **Geheimnisse** (`DNS01_IONOS_SECRET`, `DNS01_AZURE_CLIENT_SECRET`) liegen in `data/settings.json`
  wie die übrigen Zugangsdaten. Die Oberfläche zeigt sie nur maskiert. Protokoll, Status und
  API-Antworten enthalten sie nie, auch wenn die API den Schlüssel in ihrer Fehlermeldung
  wiederholt. Ein Test prüft das.
- **Keine neue Abhängigkeit:** Die DNS-Abfrage ist eine eigene Umsetzung (UDP, TCP bei TC, Verweise
  ab den Wurzelservern), dnspython ist nicht nötig. Lock-Datei und Offline-Paket bleiben unverändert.
- **Grenzen:** Die Sichtbarkeitsprüfung braucht UDP/TCP 53 nach außen. Ist das gesperrt, wartet das
  Relay das Zeitlimit ab und versucht es trotzdem. Ein CNAME auf `_acme-challenge` wird nicht
  verfolgt. HTTP-01 und DNS-01 von Hand nehmen weiter genau einen Namen ohne Wildcard.

## 0.2.56 — 2026-11-26 — Stable: Labtest-Korrekturen 0.2.43-beta bis 0.2.56-beta, Lieferkette, Offline-Paket, robuster Installer und Updater

Erste stabile Fassung seit **0.2.42**. Fasst 0.2.43-beta bis 0.2.56-beta zusammen — Einzelheiten in
deren Abschnitten unten. Nicht veröffentlicht wurden 0.2.45, 0.2.46, 0.2.50 und 0.2.51 (Labtest-
Stopps); **0.2.52-beta gibt es nur als Abbild und ist nicht zu verwenden** (siehe 0.2.53-beta).

**Upgrade-Hinweise**

- **Windows ohne nachgeladene Wurzelzertifikate** (frischer Server, auf dem nie ein Browser GitHub
  aufgerufen hat): Die alte Fassung scheitert bei „Aktualisierung prüfen“ mit
  `CERTIFICATE_VERIFY_FAILED`. Dann **einmal `windows\install.ps1`** aus dem neuen Paket als
  Administrator ausführen; danach geht das Update per Knopf. Das Upgrade per `install.ps1` behält
  Kennwort, Port, Dienst-Umgebung und Firewall-Einschränkungen.
- **Docker:** Das Abbild startet über `docker-entrypoint.sh` als root, macht `/app/data` für
  `appuser` (uid 1000) beschreibbar und wechselt dann auf `appuser`; der Dienst läuft nie als root.
  `docker compose pull && docker compose up -d`. Einen eigenen `entrypoint:` in
  `docker-compose.yml` herausnehmen; mit `user:` muss `./data` diesem Nutzer gehören.
- **Rechte auf `data\`** (Windows) werden erzwungen — durch `install.ps1` UND jetzt auch beim
  Knopf-Update (dieselbe Funktion). Fremde Einträge werden entfernt und im Status gemeldet.
- **Offline-Server:** `exo-smtp-relay-0.2.56-windows-offline.zip` (README → Offline-Installation);
  eine damit installierte Instanz aktualisiert ohne PyPI.
- **systemd-Units und der Dienst-Wrapper werden mitgezogen.** Hat ein älterer Updater getauscht
  (Knopf ab 0.2.42), holt die neue Fassung das beim ersten Start nach (**Nachbereitung, nach ca.
  90 s**): Units nach `/etc/systemd/system` (angepasste bleiben und werden gemeldet), `data\`-Rechte,
  Neustart des Windows-Dienst-Wrappers. Das Ergebnis steht unter *Aktualisierung*.
- **Windows:** Die Wiederherstellung des Dienstes (`sc failure … restart`) wird gesetzt — von
  `install.ps1` und vom Updater, sobald ein Wrapper-Neustart ansteht.

**Das Wichtigste**

- **Lieferkette:** Python-Pakete nur mit Prüfsummen (`pip --require-hashes`); Python-Installer und
  ExchangeOnlineManagement von festen Adressen mit fester SHA256 und geprüfter Authenticode-
  Signatur; kein ungeprüfter Download.
- **Windows-Offline-Paket**, vor jeder Veröffentlichung auf Windows ohne Netz geprüft; ohne grünen
  Offline-Test wird nichts veröffentlicht, auch kein Abbild.
- **`install.ps1`:** nur atomares Verschieben, Selbstheilung von Resten, Sperrtest, Rücknahme mit
  Merker und Lauf-ID, Lebenszeichen vor „Aktualisiert“, Rücknahme von Abhängigkeiten, Port und
  Firewall; Python 3.11–3.13; Dienst auch ohne Python im PATH; Dienststart mit Wiederholung.
- **Updater:** Rücknahme nur des eigenen Tauschs (Lauf-ID), ehrlich gemeldet und wiederholbar;
  offene Rücknahme wird beim Dienststart (Windows-Wrapper, systemd `ExecStartPre`) oder per Knopf
  nachgeholt; robustes Schreiben, Status wird nachgetragen; Units und Dienst-Wrapper mitgezogen.
- **TLS:** selbstsigniertes Zertifikat für FQDN und Rechnernamen, auch über 64 Zeichen.

**Im Labtest geprüft:** Windows Server 2025, Linux systemd, Docker, Offline-VM; Knopf-Update direkt
ab 0.2.42.

**Nicht geprüft:** Dateivergleich unter Windows nach Knopf ab 0.2.54 bzw. 0.2.42 (geprüft nur
`/health` und SMTP); l4c mit `windows\` als Arbeitsverzeichnis; l1/l5 nur in 0.2.55; Docker ab
0.2.42; gesperrte `status.json` unter systemd mit echtem Leser.

**Bekannt:** Nach einem Knopf-Update ab 0.2.55 bleibt `sc qfailure ExoSmtpRelay` leer, bis ein
Wrapper-Neustart ansteht oder `install.ps1` läuft — funktional ohne Folgen.

## 0.2.56-beta — 2026-11-25 — Nachbereitung durch die neue Fassung (Bootstrap)

- **Bootstrap-Lücke geschlossen** (am Code verifiziert): Der Knopf-Tausch läuft aus dem
  INSTALLIERTEN Updater — der Windows-Wrapper lädt `app\update_tausch.py`, die Phasen laufen aus
  dessen Arbeitskopie, unter systemd startet die Update-Unit `app/update_tausch.py`. Ein Updater bis
  0.2.54 kennt weder das Mitziehen der Units noch die `data\`-Rechte noch den Wrapper-Neustart; bei
  0.2.54→0.2.55 (und ab 0.2.42) kamen sie also erst beim übernächsten Update. Jetzt bereitet die
  NEUE Fassung selbst nach: Beim ersten Start (90 s verzögert, damit ein laufender alter Tausch
  fertig ist) legt sie, falls `data/update/nachbereitet.json` nicht ihre Fassung nennt, einen
  Auftrag „nachbereiten“ in den bestehenden Auftragsweg. Ausgeführt wird er mit NEUEM Code — der
  Wrapper (auch der aus 0.2.54) und der systemd-Update-Service laden `update_tausch.py` je Auftrag
  frisch. Die Nachbereitung nutzt dieselben Funktionen wie der Tausch (`einheiten_tauschen`,
  `daten_rechte_setzen`, `wrapper_abgleichen`), ist idempotent, läuft einmal je Fassung, stoppt
  den Dienst nicht und meldet ihr Ergebnis im Status; Fehler sind WARNUNGEN. Der neue Tauscher und
  `install.ps1` vermerken die Nachbereitung selbst, dann gibt es keinen zweiten Auftrag.
- **Aus dem Labtest von 0.2.55-beta:**
  - **systemd, pip in der Rücknahme beim Start (ROT 1):** `ExecStartPre` läuft mit
    `WorkingDirectory=app/`; die Rücknahme tauscht `app/` weg, pip scheiterte an `os.getcwd()`.
    Jetzt wechselt `update_tausch.py` sofort in die Installationswurzel, und pip, die Phasen,
    PowerShell und `systemctl` laufen mit festem `cwd`.
  - **systemd, alte Unit zu früh (ROT 2):** Scheiterte die Rücknahme teilweise, war die ALTE Unit
    (ohne `ExecStartPre`) schon zurück — ein Neustart holte nichts mehr nach; die 0.2.56-
    Nachbereitung deckt das nicht ab (am Platz liegt dann die alte Fassung). Jetzt kommen die
    alten Units erst, wenn die Rücknahme VOLLSTÄNDIG gelungen ist; solange sie offen ist, bleibt
    die Unit mit `ExecStartPre` stehen. Gewählt statt eines Nachhol-Pfads außerhalb der Unit,
    weil es denselben Weg nutzt, den Start ohnehin geht. `ExecStartPre=-+…`: ein Fehlschlag (etwa
    eine zurückgeholte ältere Fassung ohne den Befehl) blockiert den Start nie.
  - **Windows, Wrapper-Neustart blieb aus (ROT 3):** Die geplante Aufgabe blieb im Lab dauerhaft
    „Queued“ (im CI lief sie). Die Ursache ist von hier aus nicht belegbar — Kandidaten sind die
    Vorgaben, mit denen `schtasks /Create` Aufgaben anlegt (u. a. Start nur mit Netzstrom). Statt
    darauf zu bauen: Der Wrapper beendet sich nach dem Ablauf selbst (nach Anhalten der
    Anwendung, ohne SERVICE_STOPPED), die **SCM-Wiederherstellung** (`sc failure … restart`, setzt
    `install.ps1` und der Tauscher) startet ihn neu — ohne Aufgabenplanung. Bestätigt der neue
    Wrapper den Neustart nicht binnen 5 min, zeigt der Status eine WARNUNG mit
    `Restart-Service ExoSmtpRelay`.
  - **Status blieb auf „startet“ (gelb):** Ein nicht geschriebener Status wartet in
    `status.ausstehend.json` und wird vor jedem Ablauf, beim Dienststart und bei jeder Abfrage der
    Oberfläche nachgetragen; die Anzeige nimmt ihn sofort.
  - **Bootstrap (gelb):** bestätigt — Knopf von ≤0.2.54 auf 0.2.56 löst die Nachbereitung aus
    (kein `nachbereitet.json` vom alten Tauscher; Test).
- Bleibt bestehen und ist so gewollt: Nimmt ein ALTER Updater zurück, stellt er die alte Unit
  ohne `ExecStartPre` wieder her — das ist die alte Fassung; die nächste neue bereitet wieder nach.
- Tests: Tausch durch einen simulierten alten Tauscher, danach erster Start der neuen Fassung →
  Units, Rechte und Wrapper nachgezogen; CI „Installer echt“: 0.2.54 installiert (alter Wrapper
  läuft), Endzustand eines Tauschs durch den 0.2.54-Tauscher, Auftrag „nachbereiten“ → der alte
  Wrapper führt die NEUE Nachbereitung aus (Rechte, Wrapper-Neustart, Status).

## 0.2.55-beta — 2026-11-25 — Offene Rücknahme: Nachholen überall, robustes Schreiben, BOM

Aus dem Nachtest von 0.2.54-beta (l6 und l4b unter Windows grün).

- **systemd holte eine offene Rücknahme nie nach (ROT 1).** Ohne Weboberfläche gab es keinen Weg;
  `systemctl restart exo-smtp-relay` (das der Status empfahl) half nicht; startete die neue
  Fassung später doch, lehnte der Knopf mit 409 „Keine neuere Fassung bekannt“ ab. Jetzt:
  - **Ein gemeinsamer Pfad beim Dienststart** (`update_tausch.beim_dienststart`): Windows über
    den Dienst-Wrapper, systemd über `ExecStartPre=+… update_tausch.py nachholen` (als root, vor
    der Anwendung, blockiert den Start nie).
  - Der **Knopf beauftragt bei offener Rücknahme das Nachholen** („Rücknahme nachholen“) statt
    abzulehnen; die Seite Aktualisierung zeigt die offene Rücknahme an.
  - Der Status nennt den **plattformrichtigen** Befehl (Linux: `systemctl restart`, Windows:
    `Restart-Service` oder `install.ps1`) und kündigt an, dass der nächste Dienststart die vorige
    Fassung zurückspielt — auch wenn die neue inzwischen läuft.
- **Gesperrte status.json brach den Ablauf ab (ROT 2, Windows).** Ein fremder Leser ohne
  FileShare.Delete ließ `os.replace` mit WinError 5 scheitern; die neue Fassung lief dann ohne
  Überwachung bzw. der Status hing auf „startet“. Jetzt wiederholt `json_schreiben` mit wachsender
  Pause (bis ≈ 10 s); der Status bricht den Ablauf nie ab (Fehler auf stderr). Kritische Merker
  (Tausch-Merker, `ruecknahme_offen.json`) werden ebenso wiederholt; scheitert der Tausch-Merker
  endgültig, endet der Lauf, bevor etwas getauscht ist.
- **BOM (ROT 3).** `install.ps1` schrieb die offene Rücknahme mit `Set-Content -Encoding UTF8`
  zurück — unter PS 5.1 mit BOM; Python las `{}`, „nichts offen“, das Ereignisprotokoll meldete
  „nachgeholt“. Jetzt schreibt `install.ps1` alles für Python ohne BOM (`Write-RelayTextOhneBom`)
  und liest UTF-8 ausdrücklich; Python liest `utf-8-sig`. **Unlesbar ist nicht „nichts offen“**,
  sondern FEHLER mit Hinweis (`json_lesen_streng`); „nachgeholt“ nur bei echtem Erfolg. Ebenso
  der Tausch-Merker und die Status-Anzeige.
- **Gelb:** Nach erfolgreichem `install.ps1` bei offener Rücknahme schreibt der Installer den
  Updater-Status neu (statt weiter „UNVOLLSTÄNDIG“).
- **Windows: was bisher nur `install.ps1` setzte, zieht jetzt auch der Knopf nach.**
  (a) **Rechte auf `data\`** (erzwungen seit 0.2.45): Der Tauscher ruft nach dem Tausch
  `windows\install.ps1 -NurDatenRechte` auf — dieselbe Funktion (`Set-RelayDataAcl`) wie die
  Installation, keine zweite Kopie. Entfernte Einträge stehen im Status; Scheitern bricht nichts
  ab, steht aber als WARNUNG da. (b) **Dienst-Wrapper:** Den neuen `windows\service.py` lud Windows
  erst beim nächsten Dienstneustart. Der Wrapper meldet beim Start seine Fassung
  (`waechter.json`); weicht die Datei auf der Platte davon ab — nach einem Tausch oder einer
  Rücknahme —, plant der Tauscher den Neustart des Windows-Dienstes per einmaliger Aufgabe als
  SYSTEM ein (nicht aus dem Dienst heraus), die sich danach löscht. Der neue Wrapper bestätigt im
  Status („Dienst-Wrapper neu gestartet (Fassung …)“), eine falsche Fassung ist eine WARNUNG;
  ist der Neustart nicht einplanbar, nennt der Status `Restart-Service ExoSmtpRelay`.
  Suchlauf gegen die Git-Historie ab v0.2.42: Dienst-Umgebung, Firewall-Regeln, Startmenü-Link
  und Dienstregistrierung sind seither gleich; die Python-DLLs (0.2.46) braucht nur, wer Python
  ohne PATH-Eintrag hat — solche Installationen liefen vorher nicht.
- **Der Updater zieht die systemd-Units mit** (Dienst, Update-Service, `.path`). Bisher bekam
  wer per Knopf aktualisierte nie eine neue Unit — das Nachholen beim Start (`ExecStartPre`) wäre
  genau dort wirkungslos geblieben. Der Tauscher (root) vergleicht die Units im Paket mit
  `/etc/systemd/system`: bei Abweichung die alte nach `.update-alt/einheiten`, die neue hinein,
  `daemon-reload` — im selben Merker und mit derselben Lauf-ID wie der Tausch; die Rücknahme
  stellt die alten Units wieder her (ebenfalls mit `daemon-reload`). **Vom Betreiber angepasste
  Units werden nicht überschrieben:** Weicht die installierte Unit von jeder bekannten
  Release-Fassung ab (`linux/einheiten-bekannt.json`, aus der Git-Historie,
  `tools/einheiten_bekannt.py`), bleibt sie, und der Status meldet es. Drop-ins unter
  `*.service.d` bleiben immer unberührt. Ein Test verlangt, dass jede neue Unit-Fassung in der
  Liste steht.

## 0.2.54-beta — 2026-11-25 — Updater: Rücknahme nur des eigenen Tauschs, ehrlich und wiederholbar

Aus dem Labtest von 0.2.53-beta (sonst in allen Punkten grün).

- **Fremder Rückfallstand (l6, vor Stable nötig):** Fehlte im Merker von `.update-alt` das Feld
  „nach“ (Updater ≤ 0.2.49, auch 0.2.42 stable), galt er als der eigene. Scheiterte dann das
  Beiseitelegen des alten `.update-alt` (Sperre in `.update-alt\windows`) — bevor der neue
  Merker stand —, holte die Rücknahme `app\` aus der VORVORIGEN Fassung zurück; `windows\`
  scheiterte, gemeldet wurde „läuft weiter“. Jetzt trägt jeder Tausch eine **Lauf-ID** (wie
  `install.ps1`); zurückgenommen wird nur ein Rückfallstand mit genau dieser ID. Fehlt sie oder
  ist sie fremd, wird nichts angefasst („nichts zurückgenommen, nichts verändert“).
- **Rücknahme scheitert selbst (l4b, Doppelfehler):** Startete die neue Fassung nicht UND war
  `app\main.py` während der Rücknahme gesperrt, verwarf `ablauf()` das Ergebnis und meldete
  „zurückgerollt … läuft wieder“ — bei neuer VERSION und ohne Listener. Jetzt: Status FEHLER
  („Rücknahme UNVOLLSTÄNDIG“, Fassung am Platz, Hinweis auf `install.ps1`), und
  `data\update\ruecknahme_offen.json` hält die Rücknahme fest. Sie wird beim nächsten
  Dienststart (Windows-Dienst-Wrapper, bevor die Anwendung startet) bzw. vor dem nächsten Auftrag
  erneut versucht; ein neuer Tausch beginnt erst, wenn sie gelang. Die Rücknahme ist
  wiederholbar: schon zurückgeholte Teile werden übersprungen.
- **Abhängigkeiten in der Rücknahme:** `.update-alt` samt Merker verschwindet erst NACH pip —
  scheiterte pip, war der Merker bisher weg und ein Nachholen hätte Erfolg ohne Abhängigkeiten
  gemeldet. Der pip-Bedarf steht im Merker.
- **Reste:** `*.relay-verworfen` gescheiterter Rücknahmen räumt der nächste erfolgreiche Lauf weg;
  was sich nicht löschen lässt, steht im Status.
- **`install.ps1` und offene Rücknahme:** Der Installer verwirft eine offene Updater-Rücknahme
  vor dem Dienststart (sonst spielte der Wrapper beim Start eine ältere Fassung über die gerade
  installierte) und stellt sie bei eigenem Abbruch wieder her.
- **Kosmetik:** `Start-RelayDienst` zeigt aus `service.log` nur Zeilen ab dem Startversuch, als
  UTF-8 gelesen (bisher alte Zeilen vom Vortag in falscher Kodierung).
- systemd: Die offene Rücknahme holt der nächste Auftrag nach (Knopf) bzw. ein Aufruf von
  `update_tausch.py ablauf` ohne Auftrag.

## 0.2.53-beta — 2026-11-25 — Offline-Test im Release, Veröffentlichung nur nach grünem Offline-Test

**0.2.52-beta nicht verwenden:** Der Release-Lauf v0.2.52-beta war im Job „Offline-Paket“ rot;
ein GitHub-Release gibt es nicht, die Abbilder `ghcr.io/azitc-ac/exo-smtp-relay:0.2.52-beta`
und `:beta` wurden trotzdem gepusht (der Image-Job hing nicht vom Offline-Test ab). `:beta`
zeigt ab diesem Release wieder auf eine geprüfte Fassung; das Tag `0.2.52-beta` bleibt als
verwaiste Fassung stehen.

- **Ursache des roten Offline-Tests:** keine Regression aus 0.2.51/0.2.52 und nicht das neue
  Runner-Python 3.13.17. Dasselbe Paket, wie im Release auf Ubuntu gebaut und auf Windows
  geprüft, lief auf dem heutigen Runner grün für 795b366 (genau der rote Release-Stand),
  97cc8bf, efbf608 und den aktuellen Stand. Der rote Lauf scheiterte nach rund 22 s schnell mit
  „Failed to start service“, ohne dass die Probe service.log oder das SCM-Ereignis ausgab — die
  genaue Ursache ist deshalb nicht belegt (einmaliger Startfehler).
- **`install.ps1` startet den Dienst bis zu dreimal** (`Start-RelayDienst`, 10 s Pause) und meldet
  jeden Fehlschlag mit dem SCM-Ereignis und dem Ende von `service.log`; erst wenn alle Versuche
  scheitern, bricht der Lauf ab (Rücknahme). Ob die Fassung trägt, entscheidet weiter das
  Lebenszeichen. Gilt auch für den Start nach einer Rücknahme.
- **Release:** Die Freigabe hängt jetzt auch vom Offline-Test ab (`needs: [pakete, offline]`) —
  ist er rot, wird nichts veröffentlicht, auch kein Abbild (Test).
- **CI prüft dasselbe Paket wie das Release:** Das Offline-Paket wird wie im Release auf Ubuntu
  gebaut und auf Windows geprüft (bisher baute die CI auf Windows selbst — grün, während der
  Release-Test rot war). Bei einem Fehlschlag gibt die Probe Dienst-Konfiguration,
  `service.log`, `app.log` und die SCM-/Anwendungsereignisse aus. Für Gegenproben nimmt der
  CI-Workflow per `workflow_dispatch` einen anderen Stand (`stand`).

## 0.2.52-beta — 2026-11-25 — Rücknahme bei gleicher Fassung; Beta vor Stable

**0.2.51 wurde nicht veröffentlicht** — der Labtest fand eine Regression (unten); ihr Inhalt
(Abschnitt 0.2.51 mit der Zusammenfassung seit 0.2.42) geht hier auf. Beta, weil der Updater
(`update_tausch.py`) seit 0.2.51 geändert ist und vor Stable einen echten Knopf-Test mit
signierten Assets braucht.

- **Dieselbe Fassung noch einmal installiert, die neue wirft (Labtest Z4):** 0.2.51 meldete
  „app\: 41 fehlende Datei(en) aus der Sicherung ergaenzt“ und „Vorige Fassung zurueckgeholt“ —
  tatsächlich blieb ein Gemisch (anderer Inhalt in `app\`, 6 statt 5 Dateien in `windows\`), die
  Sicherung war weg, nichts lauschte. Ursache: Die Rücknahme hielt „Marke am Platz = Fassung im
  Merker“ für die alte Fassung und ergänzte nur; bei gleicher Fassung trägt das Neue dieselbe
  Marke. Jetzt tragen `.relay-fassung` UND der Merker eine **Lauf-ID** (GUID je
  `install.ps1`-Lauf). Trägt der Teil am Platz die Lauf-ID aus dem Merker, ist er der NEUE Stand
  dieses Laufs: immer beiseite und die Sicherung vollständig zurück. Ergänzt wird nur bei Resten
  früherer Läufe bzw. Fassungen.
- **Updater geprüft:** `update_tausch` kann diesen Pfad nicht nehmen — gleiche Fassungen tauscht
  er gar nicht (`phase_pruefen`: „nicht neuer“), und seine Rücknahme legt das Neue immer ganz
  beiseite und holt das Alte ganz zurück, ohne Ergänzen (Test).
- Tests: pwsh mit eingebauter Gegenprobe ohne Lauf-ID; CI-Job „Installer echt“: gleiche Fassung,
  kaputt — `app\` und `windows\` per Hash vorher = nachher; Gegenprobe gegen 3095166 („ergaenzt“,
  Hash anders).

## 0.2.51 — 2026-11-25 — (nicht veröffentlicht) Labtest-Korrekturen 0.2.43-beta bis 0.2.51, Lieferkette, Offline-Paket

Erste stabile Fassung seit 0.2.42. Fasst 0.2.43-beta, 0.2.44-beta, 0.2.45, 0.2.46 und 0.2.50
(alle drei nicht veröffentlicht), 0.2.47-beta, 0.2.48-beta und 0.2.49-beta zusammen — Einzelheiten in deren
Abschnitten unten. Im Labtest (Windows Server 2025, Linux, Docker) geprüft.

**Upgrade-Hinweise**

- **Windows ohne nachgeladene Wurzelzertifikate** (frischer Server, auf dem nie ein Browser
  GitHub aufgerufen hat): Die alte Fassung scheitert bei „Aktualisierung prüfen“ mit
  `CERTIFICATE_VERIFY_FAILED`. Dann **einmal `windows\install.ps1`** aus dem neuen Paket als
  Administrator ausführen; danach geht das Update per Knopf. Das Upgrade per `install.ps1`
  behält Kennwort, Port, Dienst-Umgebung und Firewall-Einschränkungen.
- **Docker:** Das Abbild startet über `docker-entrypoint.sh` als root, macht `/app/data` für
  `appuser` (uid 1000) beschreibbar und wechselt dann auf `appuser`; der Dienst läuft nie als
  root. Einfach `docker compose pull && docker compose up -d`. Wer in `docker-compose.yml`
  einen eigenen `entrypoint:` gesetzt hat, nimmt ihn heraus; mit `user:` startet der Befehl
  unverändert, `./data` muss dann diesem Nutzer gehören (NFS mit `root_squash`:
  `sudo chown -R 1000:1000 data`).
- **Bewusst gesetzte Rechte auf `data\`** (Windows) werden beim nächsten `install.ps1` entfernt
  und gemeldet: `data\` hat genau SYSTEM und Administratoren.
- **Offline-Server:** neues Paket `exo-smtp-relay-0.2.51-windows-offline.zip` (README →
  Offline-Installation). Eine damit installierte Instanz aktualisiert ohne PyPI.

**Das Wichtigste**

- **Lieferkette:** Python-Pakete nur mit Prüfsummen (`pip --require-hashes`) überall;
  Python-Installer und ExchangeOnlineManagement von festen Adressen mit fester SHA256 und
  geprüfter Authenticode-Signatur; kein ungeprüfter Download, keine pip-Selbstaktualisierung.
  Bei der Lieferketten-Arbeit aufgedeckt: `colorama` (Windows) fehlte in der Lock-Datei.
- **Windows-Offline-Paket** mit Python, 44 Wheels (Python 3.11–3.13) und ExchangeOnlineManagement;
  vor jeder Veröffentlichung auf Windows ohne Netz installiert und geprüft.
- **`install.ps1` robust beim Upgrade:** verschiebt Ordner nur atomar und nie in ein
  vorhandenes Ziel, führt Reste früherer Läufe selbst zurück, Sperrtest vor dem Austausch, Rücknahme nach dem Inhalt
  der Sicherung mit Merker, Lebenszeichen (`/health` mit der Soll-Version) vor „Aktualisiert“,
  Rücknahme von Abhängigkeiten (auch auf Fassungen mit Lock-Datei ohne Prüfsummen), Web-Port
  und Firewall nach einer Regel; Exit-Codes geprüft; `data\`-Rechte erzwungen, nie beendend.
- **Python:** nur 3.11–3.13; Dienst startet auch ohne Python im Maschinen-PATH.
- **TLS:** selbstsigniertes Zertifikat trägt FQDN und Rechnernamen, auch bei Namen über 64 Zeichen.
- **Firewall:** ein Upgrade öffnet nie weiter als vorher.

Neu in 0.2.51 (Labtest von 0.2.50 — **0.2.50 wurde nicht veröffentlicht**):

- **`Move-Item` auf einen Ordner ist unter Windows PowerShell 5.1 (Build 26100) nicht atomar.**
  Bei einer offenen Datei darin (Share Read, kein Delete) wirft es, hat aber schon einen Teil der
  Dateien verschoben. Folgen im Labtest: Der Sperrtest selbst zerlegte `app\` (14 von 102
  Dateien in `app.relay-sperrtest`), meldete „nichts verschoben“, der Dienst lief ohne Listener;
  Folgeläufe verschachtelten den Rest (`app.relay-sperrtest\app`) und scheiterten auch
  entsperrt. Mit einer offenen Datei in `.install-alt\windows` blieb `windows\` unvollständig,
  gemeldet wurde „zurückgeholt“. Jetzt:
  - Jede Verschiebung in `install.ps1` läuft über `Move-RelayPfad`: `[IO.Directory]::Move` bzw.
    `[IO.File]::Move` (ein Rename — ganz oder gar nicht) und nie auf ein vorhandenes Ziel.
    Kein `Move-Item`/`Rename-Item` mehr (Test).
  - **Selbstheilung:** Reste früherer Läufe (`*.relay-sperrtest`, auch verschachtelt;
    `*.relay-verworfen`) werden zu Beginn dateiweise zurückgeführt, mit Meldung.
  - Die Rücknahme unterscheidet über `.relay-fassung`: am Platz die NEUE Fassung → tauschen;
    ein Teil der ALTEN → fehlende Dateien aus der Sicherung ergänzen. Neue Ordnerteile bekommen
    die Marke VOR dem Inhalt.
  - Kein „erst löschen, dann ersetzen“ an Ordnern mehr: ExchangeOnlineManagement-Modul im
    Installer, Rückfall im Updater (`update_tausch.phase_zurueck`) und das Wegräumen des vorigen
    Rückfallstands — erst atomar beiseite, dann löschen, so weit es geht. Der Updater nimmt nur
    die Marke SEINES Tauschs zurück.
  - `update_tausch` verschiebt mit `os.replace` (MoveFileExW): unter Windows mit offener Datei
    atomar (Test).
- Tests: isoliert unter PS 5.1 bewegt `Move-RelayPfad` bei offener Datei 0 von 60 Dateien,
  `Move-Item` als Gegenprobe einen Teil; `os.replace` ebenso atomar. CI-Job „Installer echt“
  mit offener Datei in `app\`, `windows\` und `.install-alt\windows`, je mit Folgelauf und
  entsperrt, Gegenproben gegen aa45770 (Teilverschiebung sichtbar) mit anschließender Heilung
  ohne Handarbeit.

Aus 0.2.50 (nicht veröffentlicht; Labtest von 0.2.49-beta, Sperre IN `.install-alt\`):

- **Z1:** Ließ sich die gesicherte alte `windows\` nicht zurückholen (Programm mit
  Arbeitsverzeichnis darin), war die neue schon beiseite — kein `windows\` am Platz, Dienst
  gestoppt. Jetzt kommt die neue zurück an ihren Platz; der Dienst startet mit dem, was da
  ist, und die Rücknahme meldet „UNVOLLSTAENDIG“.
- **Z2:** Nach einem ERFOLGREICHEN Upgrade blieb bei derselben Sperre ein leerer Rest von
  `.install-alt\windows` ohne Merker und ohne Meldung; ein Folgelauf hielt ihn für eine
  Sicherung und verwarf das gute `windows\`. Jetzt: zurückgeholt wird nur, was dieser Lauf
  verschoben hat oder ein Merker nennt, ein leerer Ordner ist nie eine Sicherung, der Merker
  wird nach einem Erfolg zuerst gelöscht, ein nicht löschbarer Rest ist eine WARNUNG (Exit 0),
  und ein Folgelauf bricht an ihm ab, ohne etwas zu verschieben.
- Tests: CI-Job „Installer echt“ mit Z1 und Z2 (Sperre greift erst während des
  Lebenszeichens), jeweils mit Folgelauf und Gegenprobe gegen 97cc8bf.

## 0.2.49-beta — 2026-11-25 — Rücknahme: hash-lose Vorfassung, Sperre während des Lebenszeichens

Aus dem großen Labtest von 0.2.48-beta (sonst grün: offline ohne Netz auf Server 2025,
Offline-Update, Knopf-Update, Rollback, Python 3.11/3.13 ohne PATH, FQDN 94 Zeichen,
Manipulationsproben):

- **Rücknahme auf eine Fassung mit Lock-Datei ohne Prüfsummen (e6a):** 0.2.42 und 0.2.44-beta
  haben keine Hashes; die Rücknahme installierte deren Lock mit `--require-hashes`, pip brach
  ab („Hashes are required“), die neuen Pakete blieben (idna). Jetzt wird eine Lock ohne
  Prüfsummen so installiert wie damals, ohne den Schalter — in `install.ps1` UND im Updater
  (`update_tausch.pip_argumente`, Rückfall-Phase), weiterhin derselbe Weg. Jede Lock ab
  0.2.47 trägt Prüfsummen (Test).
- **Sperre erst während der Lebenszeichen-Wartezeit (e5):**
  a) Dienst-Umgebung und Firewall folgen jetzt EINER Regel: Port zurück, wenn `app\`
     zurückkam (das ist der Code, der läuft), sonst beide nicht — mit Meldung. Bisher ging die
     Firewall zurück, `WEBUI_PORT` nicht.
  b) Merker `.install-alt\zurueckholen.json` (Teile, Fassung, Port): Der nächste Lauf holt
     diese Teile zurück — auch über die verworfene neue Fassung am Platz — oder bricht ab,
     ohne die Sicherung anzufassen. Bisher hielt er die neue `windows\` für „am Platz“ und
     löschte die Sicherung der alten. Jeder kopierte Ordnerteil trägt `.relay-fassung`; die
     Rücknahme meldet, wenn am Platz ein Teil einer anderen Fassung liegt.
- Tests: CI-Job „Installer echt“ mit e6a (Vorfassung ohne Prüfsummen, neue mit idna 3.10
  und kaputter `main.py`) und e5 (Sperre greift erst während des Wartens, Folgelauf unter
  Sperre, Entsperren), jeweils mit Gegenprobe gegen efbf608 (0.2.48-beta).
- Offline-Paket: 44 Wheels (seit colorama in 0.2.47-beta), nicht 43 wie zuerst berichtet.

## 0.2.48-beta — 2026-11-25 — Stable-Kandidat: Rücknahme des Windows-Installers korrigiert

**0.2.46 wurde nicht veröffentlicht** — der Labtest (2ea9192) stoppte sie an der neuen
Rücknahme. Ihr Inhalt (unten) und der von 0.2.47-beta (Lieferkette, Offline-Paket) gehen hier auf.

- **Gesperrter Ordner beim Upgrade (Labtest e3):** Lag ein Programm mit Arbeitsverzeichnis in
  `windows\`, war `app\` schon nach `.install-alt\` verschoben, `windows\` nicht. Die Rücknahme
  meldete fälschlich „unveraendert“, `app\` fehlte, der Dienst startete nicht; ein zweiter Lauf
  **löschte die einzige Sicherung** („Rest eines frueheren Laufs entfernt“). Jetzt:
  Sperrtest VOR dem ersten Verschieben (jeder Teil probeweise umbenannt) — ist einer gesperrt,
  wird nichts verschoben und das stimmt dann auch; die Rücknahme richtet sich nach dem
  tatsächlichen Inhalt von `.install-alt\`; ein Rest früherer Läufe wird erst zurückgeholt,
  gelöscht nur, wenn alle Teile am Platz sind; das Neue wird vor dem Zurückschieben beiseite
  umbenannt statt gelöscht, und eine unvollständige Rücknahme lässt die Sicherung liegen und sagt es.
- **Neue Fassung startet, lauscht aber nicht (Labtest e4):** `main.py` warf beim Import, der
  Dienst war „Running“, `/health` blieb leer — trotzdem Exit 0 und „Aktualisiert“. Jetzt wartet
  der Installer bis 60 s auf `/health` des wirksamen Ports mit der Soll-Version; sonst Rücknahme
  der Fassung **samt Web-Port in der Dienst-Umgebung und in der Firewall** (bisher nicht
  zurückgenommen).
- **c2-Meldungen:** „entfernt auf …“ und „Vererbung wieder an …“ erscheinen erst, wenn das
  Zurücksetzen gelungen ist.
- Tests: Sperre, Rest und Rücknahme nach Inhalt in pwsh; im CI-Job „Installer echt“ (PS 5.1)
  e3 echt (Arbeitsverzeichnis eines Prozesses in `windows\`, zweimal) und e4 echt (kaputte
  `main.py`, `-WebPort 9443`), jeweils mit Gegenprobe gegen 2ea9192.
- Nicht in der CI nachgestellt: pip scheitert erst nach teilweisem Paketwechsel (pip löst vor
  dem Installieren vollständig auf und prüft alle Prüfsummen vorab; ein Abbruch mitten in der
  Installation lässt sich ohne Eingriff in pip nicht erzwingen). Die Rücknahme installiert in
  jedem Fall die Lock-Datei der vorigen Fassung neu.

## 0.2.47-beta — 2026-11-25 — Lieferkette gehärtet, Windows-Offline-Paket

**Lieferkette: Beim Kunden wird nichts Ungeprüftes geladen oder ausgeführt.**

- **Python-Installer**: feste Adresse (python.org, 3.12.10 amd64) mit fest eingetragener
  SHA256; vor dem Start Authenticode `Valid` mit Signierer „Python Software Foundation“,
  sonst Abbruch mit klarer Meldung. Bisher lief der Download ungeprüft. Nur noch x64:
  cryptography hat kein Windows-ARM64-Wheel (Windows auf ARM nutzt die x64-Emulation).
- **Python-Pakete nur mit Prüfsummen**: `app/requirements.lock` trägt zu jeder
  Anforderung die SHA-256 ihrer Wheels für win_amd64 und manylinux x86_64/aarch64,
  Python 3.11–3.13 (82 Prüfsummen für 34 Pakete, Quelle PyPI-JSON, `tools/lock_hashes.py`). Installiert
  wird überall mit `pip --require-hashes --only-binary :all:`: `install.ps1`, Updater
  (`update_tausch.phase_pip`), Dockerfile, systemd-Einrichtung, CI. Keine Quellpakete,
  keine pip-Selbstaktualisierung aus dem Netz mehr. `PyJWT[crypto]` steht mit Extra in der
  Lock-Datei (msal fordert es; im Hash-Modus sonst Abbruch). **`colorama` fehlte in der Lock-Datei**: click
  fordert es unter Windows; pip lud es bisher ungepinnt nach — der Hash-Modus hat es in
  der Windows-CI aufgedeckt, jetzt 0.4.6 mit Prüfsumme (nur Windows). CI prüft die Prüfsummen gegen
  PyPI und dass jede Zielplattform ein Wheel hat. Fassungen unverändert (Gateway-Stand).
- **ExchangeOnlineManagement** kommt nicht mehr über `Install-Module` (unpinned, samt
  NuGet-Bootstrap), sondern als festgelegtes nupkg 3.10.1 von der PowerShell Gallery:
  SHA256 fest, jede Skriptdatei mit gültiger Microsoft-Signatur, erst dann nach
  `Program Files\WindowsPowerShell\Modules`. Fassung wie Dockerfile und Gateway.
- `install.ps1` hat genau einen Download-Weg (`Get-RelayDownload`); install.ps1 und der
  Updater bauen ihre pip-Argumente gleich (geprüft).

**Neu: `exo-smtp-relay-<version>-windows-offline.zip`** (Windows x64, ~91 MB): App, Python-
Installer, alle Wheels für win_amd64 (Python 3.11–3.13), ExchangeOnlineManagement-nupkg und
`inhalt.json`. Steht in `SHA256SUMS`, ist signiert wie die anderen. `install.ps1` erkennt
den Inhalt, meldet „Offline-Installation“ und installiert ohne Internet (`pip --no-index`).
Eine offline installierte Instanz lädt beim Update das Offline-Paket und installiert dessen
Wheels ohne PyPI; Lebenszeichen, Rückfall und `data\` wie gehabt. Anzeige unter
*Aktualisierung → Installationsart*. Die Release-CI baut das Paket aus festen Quellen mit
Prüfsummen und installiert es vor der Veröffentlichung auf Windows ohne Netz für pip
(Job „Offline-Paket“; ebenso in der normalen CI). Einzige Online-Komponente bleibt Exchange
Online selbst. Ein Linux-Offline-Archiv folgt später.

## 0.2.46 — 2026-11-24 — Stable-Kandidat: Labtest-Korrekturen aus 0.2.43-beta, 0.2.44-beta, 0.2.45 und 0.2.46

**0.2.45 wurde nicht veröffentlicht** — der Labtest hat sie gestoppt (siehe unten). Ihr
Inhalt geht vollständig in 0.2.46 auf.

Fasst die Betas 0.2.43 und 0.2.44 zusammen (Einzelheiten in deren Abschnitten unten).

**Wichtig für bestehende Windows-Installationen:** Fehlen auf dem Server die
nachgeladenen Wurzelzertifikate (typisch: frischer Server, auf dem nie ein Browser
GitHub aufgerufen hat), scheitert „Aktualisierung prüfen“ der alten Fassung mit
`CERTIFICATE_VERIFY_FAILED`. Dann **einmal `windows\install.ps1`** aus dem neuen
Paket als Administrator ausführen. Danach geht das Update per Knopf. Das Upgrade
per `install.ps1` behält Kennwort, Port, Dienst-Umgebung und Firewall-Einschränkungen.

Neu in 0.2.46 (Labtest von 0.2.45, Windows PowerShell 5.1):

- **`install.ps1` brach an einer Datei ab, die der Administrator nicht zurücksetzen
  darf** (Eigentümer SYSTEM, nur SYSTEM:F). `icacls /reset` schreibt dann „Access is
  denied“ nach stderr; mit `2>&1` und `ErrorActionPreference=Stop` wird das unter PS 5.1
  zum beendenden Fehler. Folge: Exit 1, Dienst blieb **gestoppt**, VERSION schon neu —
  ein halbes Upgrade. Jetzt läuft icacls mit lokal `Continue`, ohne `/C` (damit meldete
  icacls Exit 0 trotz „Failed processing“, die WARNUNG kam nie), Exit-Code und Ausgabe
  stehen in der Warnung. Der ganze Rechte-Schritt ist nie beendend.
- **Abbruch nach dem Dienststopp: Rücknahme statt halbem Upgrade.** Die bisherige
  Fassung (`app\`, `windows\`, `VERSION` …) wandert vor dem Kopieren nach
  `.install-alt\`. Scheitert danach irgendein Schritt, kommt sie zurück, die
  Abhängigkeiten werden auf ihre Lock-Datei zurückgesetzt und der Dienst startet
  wieder, wenn er vorher lief — mit klarer Meldung („ABBRUCH im Schritt …“, „Vorige
  Fassung … zurückgeholt“, „Dienst laeuft wieder“). `data\` und `venv\` bleiben an Ort und Stelle.
- **Exit-Codes werden geprüft**: venv, pip, `service.py install/update` und
  `dienst_umgebung.py` meldeten nach einem Fehler trotzdem „OK“. Ein Fehler führt jetzt
  zum Abbruch samt Rücknahme. Läuft der Dienst nach dem Start nicht, ebenso.
  Nicht beendend bleiben: pip-Selbstaktualisierung, pywin32-Nachinstallation und
  ExchangeOnlineManagement (jeweils WARNUNG).
- **Python nur 3.11 bis 3.13.** `py -3` nimmt die höchste installierte Fassung — im
  CI-Lauf 3.15, für die pywin32 311 kein Wheel hat; pip scheiterte (bisher unbemerkt,
  weil der Exit-Code nicht geprüft wurde). Der Installer fragt jetzt `py -3.13`,
  `-3.12`, `-3.11` vor `py -3` und `python` und nimmt nur Fassungen, für die
  `requirements.lock` Wheels hat; `-PythonExe` mit anderer Fassung bricht mit klarer
  Meldung ab. Ohne passende Fassung bietet er wie bisher Python 3.12 an.
- **Dienst startet auch mit Python ohne PATH-Eintrag.** `pythonservice.exe` liegt im
  venv-Stamm und lädt `python3XX.dll` nur über den Maschinen-PATH; fehlt die
  Python-Basis dort (Installation ohne „Add python.exe to PATH“, per `py` gefunden),
  hing der Dienst beim Start bis zum Zeitlimit (CI: SCM-Ereignis 7009). Jetzt legt der
  Installer in diesem Fall die DLLs neben `pythonservice.exe` (gemerkt in
  `venv\relay-dienst-dlls.txt`) und entfernt genau diese Kopien wieder, sobald die Basis
  im PATH steht.
- **Selbstsigniertes Zertifikat bei langem Rechnernamen.** Ein FQDN über 64 Zeichen
  (CI-Runner: 65; Azure-VMs kommen leicht dahin) ließ die Erzeugung scheitern („Attribute's
  length must be >= 1 and <= 64“) — die Oberfläche lief ohne TLS. Jetzt ist der CN der
  erste Name bis 64 Zeichen (sonst `exo-smtp-relay.local`), alle Namen stehen wie bisher
  im SAN. Dasselbe für den CSR an Let's Encrypt (DNS-01): Namen über 64 Zeichen nur im SAN.
- Tests: Der Nachbau ersetzt nicht mehr die icacls-Funktion, sondern nur icacls.exe
  selbst (Exit-Codes wie gemessen); Gegenproben für 0.2.45, „/C zurück“ und „EAP
  zurück“; unter echtem PS 5.1 ein echter icacls-Fehler samt Gegenprobe. Neuer CI-Job
  **„Installer echt“** (windows-latest, PS 5.1): Erstinstallation; Upgrade mit
  SYSTEM-eigener Datei in `data\` (Exit 0, WARNUNG „Exit 5: … Access is denied“);
  Gegenprobe mit 0.2.45 (bricht an derselben Datei ab, Dienst gestoppt, VERSION schon
  neu — der Labtest-Fehler; ebenso bei einem an pip scheiternden Upgrade); dasselbe
  kaputte Upgrade mit 0.2.46 (Rücknahme, Dienst läuft mit alter Fassung).

Aus 0.2.45 (nicht veröffentlicht):

- **Windows: Rechte auf `data\` werden erzwungen** (sicherheitsrelevant). Bisher
  schaltete `install.ps1` nur die Vererbung ab und fügte SYSTEM und Administratoren
  hinzu; ausdrücklich gesetzte fremde Einträge (Labtest: „Backup Operators: Read“)
  blieben stehen, die Meldung „nur fuer SYSTEM und Administratoren lesbar“ stimmte
  nicht. Jetzt hat `data\` genau SYSTEM und Administratoren (Vollzugriff, vererbt auf
  Unterordner und Dateien); jeder andere Eintrag wird entfernt und als
  „entfernt: <Konto> <Rechte>“ gemeldet. Unterobjekte mit eigenen Einträgen oder
  abgeschalteter Vererbung werden per `icacls /reset` auf „nur geerbt“
  zurückgesetzt und ebenfalls gemeldet. Konten per SID, damit es auch auf
  deutschsprachigem Windows greift. Der Dienst läuft als LocalSystem und behält Zugriff.
- **Windows: Portwechsel per `-WebPort` behält die Einschränkungen der Web-Regel.**
  Bei mehreren Web-Regeln gewann bisher die zum neuen Port passende, auch wenn sie
  offen war — die eingeschränkte wurde gelöscht. Jetzt bleibt die am stärksten
  eingeschränkte (RemoteAddress, Profil) und bekommt nur den neuen Port; eine
  offene Regel gewinnt nie gegen eine eingeschränkte.

Aus 0.2.43-beta: Docker startet mit root-eigenem `./data`; Windows lädt für
ausgehende TLS-Verbindungen Systemspeicher UND certifi (behebt
`CERTIFICATE_VERIFY_FAILED`); das selbstsignierte Zertifikat trägt FQDN und
Rechnernamen.

Aus 0.2.44-beta: `install.ps1` meldet den Kennwortstand richtig, führt die
Dienst-Umgebung zusammen statt sie zu überschreiben, behält ohne `-WebPort` den
bisherigen Port und öffnet die Firewall beim Upgrade nicht weiter als vorher;
`service.log` in UTF-8.

**Zu tun:** Windows-Installationen ohne nachgeladene Wurzelzertifikate einmal
`install.ps1` ausführen (siehe oben), sonst nichts. Wer auf `data\` bewusst eigene
Einträge gesetzt hatte: Sie werden beim nächsten `install.ps1` entfernt und gemeldet.

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
