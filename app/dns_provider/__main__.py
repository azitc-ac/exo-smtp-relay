# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Alexander Zarenko / Alexander Zarenko IT Consulting (AZITC)
# Herkunft: EXO SMTP Relay (github.com/azitc-ac/exo-smtp-relay), app/dns_provider/.
"""``python -m dns_provider pruefen`` | ``schreiben <fassung>`` (aus app/ heraus)."""
import sys

from . import manifest_berechnen, manifest_lesen, manifest_schreiben

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "schreiben":
        manifest_schreiben(sys.argv[2])
        print("KATALOG_VERSION geschrieben:", sys.argv[2])
    else:
        fassung, soll = manifest_lesen()
        ist = manifest_berechnen()
        abw = sorted(set(soll) ^ set(ist) | {d for d in soll if d in ist and soll[d] != ist[d]})
        print(f"Katalog {fassung}:", "OK" if not abw else "ABWEICHUNG " + ", ".join(abw))
        sys.exit(1 if abw else 0)
