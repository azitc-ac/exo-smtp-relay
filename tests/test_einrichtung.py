"""Der Einrichtungsassistent — ein Klick, fünf Dinge im Hintergrund.

Microsoft Graph wird über `httpx.MockTransport` nachgestellt: Der Test prüft,
WAS der Assistent anlegt (App mit genau Exchange.ManageAsApp, Service
Principal, Zustimmung, Rolle, Zertifikat, Rückadresse), nicht ob Microsoft
antwortet.
"""
import asyncio
import base64
import json
import sys
from pathlib import Path

import httpx
import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))


class _Graph:
    """Ein winziger Tenant: leer beim Start, merkt sich, was angelegt wird."""

    def __init__(self):
        self.apps: list[dict] = []
        self.sps: list[dict] = []
        self.anfragen: list[tuple[str, str, dict | None]] = []
        self.key_credentials: dict[str, list] = {}
        self.bootstrap_uris: list[str] = ["http://localhost:8080/auth/callback"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        pfad = request.url.path
        body = json.loads(request.content) if request.content else None
        self.anfragen.append((request.method, pfad + ("?" + request.url.query.decode() if request.url.query else ""), body))
        q = request.url.params
        if pfad == "/v1.0/organization":
            return httpx.Response(200, json={"value": [{"id": "tenant-1", "verifiedDomains": [
                {"name": "firma.de", "isInitial": False}, {"name": "firma.onmicrosoft.com", "isInitial": True}]}]})
        if pfad == "/v1.0/applications" and request.method == "GET":
            f = q.get("$filter", "")
            if "appId eq 'boot-1'" in f:
                return httpx.Response(200, json={"value": [{"id": "boot-obj", "publicClient": {"redirectUris": self.bootstrap_uris}}]})
            name = f.split("'")[1].replace("''", "'")
            return httpx.Response(200, json={"value": [{"id": a["id"], "appId": a["appId"]} for a in self.apps if a["displayName"] == name]})
        if pfad == "/v1.0/applications" and request.method == "POST":
            app = {"id": f"obj-{len(self.apps)+1}", "appId": f"app-{len(self.apps)+1}", **body}
            self.apps.append(app)
            return httpx.Response(201, json=app)
        if pfad.startswith("/v1.0/applications/") and request.method == "PATCH":
            obj = pfad.rsplit("/", 1)[1]
            if obj == "boot-obj":
                self.bootstrap_uris = body["publicClient"]["redirectUris"]
            else:
                self.key_credentials[obj] = body["keyCredentials"]
            return httpx.Response(204)
        if pfad == "/v1.0/servicePrincipals" and request.method == "GET":
            app_id = q.get("$filter", "").split("'")[1]
            if app_id == "00000002-0000-0ff1-ce00-000000000000":
                return httpx.Response(200, json={"value": [{"id": "exo-sp"}]})
            return httpx.Response(200, json={"value": [{"id": s["id"]} for s in self.sps if s["appId"] == app_id]})
        if pfad == "/v1.0/servicePrincipals" and request.method == "POST":
            sp = {"id": f"sp-{len(self.sps)+1}", **body}
            self.sps.append(sp)
            return httpx.Response(201, json=sp)
        if pfad.endswith("/appRoleAssignments") or pfad == "/v1.0/roleManagement/directory/roleAssignments":
            return httpx.Response(201, json={"id": "x"})
        return httpx.Response(404, text=f"unbekannt: {request.method} {pfad}")


@pytest.fixture
def graph(einstellungen, monkeypatch, tmp_path):
    import auth_cert
    import setup_wizard
    monkeypatch.setattr(auth_cert, "PFX_PATH", tmp_path / "auth.pfx")
    g = _Graph()
    monkeypatch.setattr(setup_wizard, "_transport", httpx.MockTransport(g.handler))
    einstellungen.update({"GATEWAY_NAME": "EXO SMTP Relay", "BOOTSTRAP_CLIENT_ID": "boot-1",
                          "EXO_SMARTHOST": "", "PUBLIC_HOSTNAME": "relay.firma.de"})
    return g


def test_ein_login_richtet_alles_ein(graph, einstellungen):
    import auth_cert
    import setup_wizard
    ergebnis = asyncio.run(setup_wizard.run_post_auth_setup("token"))

    # Tenant erkannt, Smarthost abgeleitet
    assert einstellungen["TENANT_ID"] == "tenant-1"
    assert einstellungen["TENANT_DOMAIN"] == "firma.onmicrosoft.com"
    assert einstellungen["EXO_SMARTHOST"] == "firma.mail.protection.outlook.com"

    # Genau eine App mit genau einer Berechtigung — kein Graph, kein Geheimnis
    assert len(graph.apps) == 1
    app = graph.apps[0]
    assert app["displayName"] == "EXO SMTP Relay"
    assert app["signInAudience"] == "AzureADMyOrg"
    assert app["requiredResourceAccess"] == [{
        "resourceAppId": "00000002-0000-0ff1-ce00-000000000000",
        "resourceAccess": [{"id": "dc50a0fb-09a3-484d-be87-e023b12c6440", "type": "Role"}]}]
    assert not any("addPassword" in p for _, p, _ in graph.anfragen), "kein Client-Secret"
    assert einstellungen["CLIENT_ID"] == "app-1"
    assert ergebnis["app_id"] == "app-1"

    # Service Principal, Zustimmung, Rolle
    assert len(graph.sps) == 1
    zustimmung = [b for m, p, b in graph.anfragen if p.endswith("/appRoleAssignments")]
    assert zustimmung == [{"principalId": "sp-1", "resourceId": "exo-sp",
                           "appRoleId": "dc50a0fb-09a3-484d-be87-e023b12c6440"}]
    rolle = [b for m, p, b in graph.anfragen if p == "/v1.0/roleManagement/directory/roleAssignments"]
    assert rolle and rolle[0]["roleDefinitionId"] == "29232cdf-9323-42fd-ade2-1d097af3e4de"

    # Zertifikat erzeugt und hochgeladen — der hochgeladene Schlüssel IST der erzeugte
    assert auth_cert.PFX_PATH.exists()
    hochgeladen = graph.key_credentials["obj-1"][0]
    assert hochgeladen["type"] == "AsymmetricX509Cert"
    assert base64.b64decode(hochgeladen["key"]) == auth_cert.public_cer()
    assert einstellungen["AZURE_APP_CREATED"] is True

    # Rückadresse dieses Dienstes an der Login-App nachgetragen
    assert "https://relay.firma.de:8080/auth/callback" in graph.bootstrap_uris
    assert einstellungen["BOOTSTRAP_REDIRECT_URIS"] == graph.bootstrap_uris


def test_zweiter_lauf_legt_nichts_doppelt_an(graph, einstellungen):
    import setup_wizard
    asyncio.run(setup_wizard.run_post_auth_setup("token"))
    asyncio.run(setup_wizard.run_post_auth_setup("token"))
    assert len(graph.apps) == 1
    assert len(graph.sps) == 1
    assert graph.bootstrap_uris.count("https://relay.firma.de:8080/auth/callback") == 1


def test_vorhandener_smarthost_bleibt(graph, einstellungen):
    """Wer den Rückweg bewusst anders gesetzt hat (Port 587, anderer Host),
    bekommt ihn vom Login nicht überschrieben."""
    import setup_wizard
    einstellungen["EXO_SMARTHOST"] = "eigen.example"
    asyncio.run(setup_wizard.run_post_auth_setup("token"))
    assert einstellungen["EXO_SMARTHOST"] == "eigen.example"


def test_rueckadresse_erst_nach_bekanntgabe(einstellungen):
    """Beim allerersten Login kennt die Login-App die HTTPS-Rückadresse nicht —
    dann bleibt es beim Localhost-Weg. Danach läuft es im Popup."""
    from webui.routen import einrichtung
    einstellungen["BOOTSTRAP_REDIRECT_URIS"] = []
    assert einrichtung._setup_redirect_uri(False).startswith("http://localhost:")
    einstellungen["BOOTSTRAP_REDIRECT_URIS"] = ["https://relay.firma.de:8080/auth/callback"]
    assert einrichtung._setup_redirect_uri(False) == "https://relay.firma.de:8080/auth/callback"
    assert einrichtung._setup_redirect_uri(True).startswith("http://localhost:"), "ausdrücklich erzwungen"


def test_pkce_sitzung_ist_einmalig():
    import pkce
    state, url = pkce.create_session("http://localhost:8080/auth/callback")
    assert "code_challenge=" in url and f"state={state}" in url
    assert pkce.pop_session(state)["redirect_uri"] == "http://localhost:8080/auth/callback"
    assert pkce.pop_session(state) is None, "ein Code darf nur einmal eingelöst werden"
