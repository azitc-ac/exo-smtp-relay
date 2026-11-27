# DNS-Provider-Katalog — ändern

Der Katalog (`*.py` in diesem Ordner) ist in **EXO SMTP Relay** und im
**EXO Signature Gateway** inhaltsgleich. Herkunft ist das Relay
(github.com/azitc-ac/exo-smtp-relay, `app/dns_provider/`); das Gateway übernimmt
ihn unverändert.

| Datei | Inhalt |
|---|---|
| `KATALOG_VERSION` | Fassung + SHA-256 je `*.py` (LF-normalisiert) |
| `FASSUNGEN` | Register: jede je veröffentlichte Fassung mit Fingerabdruck, nur anhängen |

## Ablauf bei jeder Änderung an einer `*.py`

1. Änderung im **Relay** machen (Herkunft), nicht zuerst im Gateway.
2. Fassung hochzählen (SemVer: Fehlerbehebung `1.0.x`, neuer Provider/neue
   Funktion `1.x.0`, inkompatible Schnittstelle `x.0.0`).
3. Manifest neu schreiben, aus `app/` heraus:

       python -m dns_provider schreiben 1.0.1
       python -m dns_provider pruefen

4. Tests laufen lassen: `pytest tests/test_dns01_provider.py -k katalog`.
   Der Test `test_katalog_fassung_im_register` wird rot und nennt die Zeile,
   die in `FASSUNGEN` **angehängt** wird (`1.0.1  <fingerabdruck>`).
5. Committen, pushen, Relay-Release wie üblich.
6. **Gateway zieht nach:** den ganzen Ordner (`*.py`, `KATALOG_VERSION`,
   `FASSUNGEN`, `LIESMICH.md`) unverändert übernehmen; dessen Gegenstück-Test
   prüft dieselben Regeln.

## Was die Tests verhindern

- Datei geändert, Manifest nicht neu geschrieben → `test_manifest_passt_zum_inhalt`.
- Manifest neu geschrieben, Fassung aber nicht angehoben (oder eine alte
  Fassung mit neuem Inhalt) → `test_katalog_fassung_im_register`.
- Eine Zeile in `FASSUNGEN` nachträglich umgeschrieben, oder eine früher
  committete Fassung trägt heute anderen Inhalt → `test_katalog_fassungen_wie_in_der_historie`
  (braucht die Git-Historie; im flachen CI-Checkout prüft er nur, was da ist).
- Fremde Importe, fehlender Lizenzkopf, Datei nicht im Paket → die übrigen
  `test_katalog_*`/`test_lizenzkopf_*` in `tests/test_dns01_provider.py`.
