"""Der Inbound-Connector-Skript wechselt sauber zwischen Adress- und Zertifikatsvariante."""
import re
from pathlib import Path

SKRIPT = Path(__file__).resolve().parent.parent / "app" / "scripts" / "setup_relay_connector.ps1"


def test_zertifikatsvariante_raeumt_alte_adressen_ab():
    text = SKRIPT.read_text(encoding="utf-8-sig")
    block = re.search(r"aktualisiere \(Zertifikatsvariante\)\"\s*(.*?)\}\s*else", text, re.S).group(1)
    assert "-SenderIPAddresses $null" in block, "sonst bleibt nach einem Wechsel Adresse UND Zertifikatsname stehen"


def test_adressvariante_raeumt_den_zertifikatsnamen_ab():
    text = SKRIPT.read_text(encoding="utf-8-sig")
    block = re.search(r"aktualisiere \(Adressvariante\)\"\s*(.*?)\}\s*else", text, re.S).group(1)
    assert "-TlsSenderCertificateName $null" in block
