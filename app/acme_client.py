"""
Minimal ACME v2 client — RFC 8555 + RFC 8823 email-reply-00.
Dependencies: cryptography, httpx (both already in requirements.txt).
No certbot/josepy needed.
"""
import base64
import hashlib
import json
import logging

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

import settings_store

log = logging.getLogger(__name__)


def _acme_proxy() -> str | None:
    """
    Optional residential-proxy URL for ACME/CA traffic only (settings ACME_HTTP_PROXY).
    Some CAs (confirmed: CASTLE) reject finalize() when the request originates from a
    datacenter IP (Azure and other cloud ASNs) — reproduced with a 500 FileNotFoundError,
    identical across Azure and an unrelated third-party datacenter proxy. A residential
    IP is the only egress type that has worked reliably in testing (2026-07-02).
    """
    return (settings_store.get("ACME_HTTP_PROXY") or "").strip() or None


# ── Base64url helpers ─────────────────────────────────────────────────────────

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (pad % 4))


# ── JWK / thumbprint ──────────────────────────────────────────────────────────

def ec_key_to_jwk(key: ec.EllipticCurvePrivateKey) -> dict:
    """Return the public JWK dict for an EC P-256 key (sorted, canonical)."""
    nums = key.public_key().public_numbers()
    return {
        "crv": "P-256",
        "kty": "EC",
        "x": b64url(nums.x.to_bytes(32, "big")),
        "y": b64url(nums.y.to_bytes(32, "big")),
    }


def jwk_thumbprint(key: ec.EllipticCurvePrivateKey) -> str:
    """JWK SHA-256 thumbprint (RFC 7638)."""
    canonical = json.dumps(ec_key_to_jwk(key), separators=(",", ":"), sort_keys=True).encode()
    return b64url(hashlib.sha256(canonical).digest())


# ── ES256 signing ─────────────────────────────────────────────────────────────

def _es256_sign(key: ec.EllipticCurvePrivateKey, data: bytes) -> bytes:
    """ECDSA-P256-SHA256 → raw r||s (64 bytes, JOSE format)."""
    der = key.sign(data, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


# ── Key authorization (RFC 8823 §3) ──────────────────────────────────────────

def compute_key_authorization(
    token_part1: str,
    token_part2: str,
    account_key: ec.EllipticCurvePrivateKey,
) -> str:
    """
    CASTLE email-reply-00: binary byte concatenation (NOT dot-separated strings).
      full_token      = b64url(decode(token_part1) + decode(token_part2))
                        subject-token bytes FIRST, then API-token bytes
      key-authz       = full_token + "." + jwk_thumbprint
      email-response  = base64url(SHA-256(key-authz))
    Source: polhenarejos/acme_email certbot_castle/plugins/castle/utils.py
    """
    full_token = b64url(b64url_decode(token_part1) + b64url_decode(token_part2))
    key_authz = f"{full_token}.{jwk_thumbprint(account_key)}"
    return b64url(hashlib.sha256(key_authz.encode()).digest())


# ── ACME v2 Client ────────────────────────────────────────────────────────────

class AcmeClient:
    """
    Async ACME v2 client supporting email-reply-00 (RFC 8823).
    All state is in the caller — this class is intentionally stateless
    except for directory cache and nonce.
    """

    def __init__(
        self,
        directory_url: str,
        account_key: ec.EllipticCurvePrivateKey,
        account_url: str = "",
    ):
        self.directory_url = directory_url
        self.account_key = account_key
        self.account_url = account_url
        self._directory: dict = {}
        self._nonce: str = ""

    # ── Internal plumbing ─────────────────────────────────────────────────────

    async def _fetch_directory(self) -> dict:
        if not self._directory:
            async with httpx.AsyncClient(timeout=15, proxy=_acme_proxy()) as c:
                r = await c.get(self.directory_url)
                r.raise_for_status()
                self._directory = r.json()
        return self._directory

    async def _new_nonce(self) -> str:
        d = await self._fetch_directory()
        async with httpx.AsyncClient(timeout=10, proxy=_acme_proxy()) as c:
            r = await c.head(d["newNonce"])
            return r.headers["Replay-Nonce"]

    async def _get_nonce(self) -> str:
        if self._nonce:
            n, self._nonce = self._nonce, ""
            return n
        return await self._new_nonce()

    def _jws(self, url: str, payload: dict | None, nonce: str) -> dict:
        header: dict = {"alg": "ES256", "nonce": nonce, "url": url}
        if self.account_url:
            header["kid"] = self.account_url
        else:
            header["jwk"] = ec_key_to_jwk(self.account_key)

        protected_b64 = b64url(json.dumps(header, separators=(",", ":")).encode())
        if payload is None:
            payload_b64 = ""         # POST-as-GET
        else:
            payload_b64 = b64url(json.dumps(payload, separators=(",", ":")).encode())

        sig = _es256_sign(self.account_key, f"{protected_b64}.{payload_b64}".encode())
        return {
            "protected": protected_b64,
            "payload": payload_b64,
            "signature": b64url(sig),
        }

    async def _post(self, url: str, payload: dict | None) -> httpx.Response:
        nonce = await self._get_nonce()
        body = self._jws(url, payload, nonce)
        async with httpx.AsyncClient(timeout=20, proxy=_acme_proxy()) as c:
            r = await c.post(
                url, json=body,
                headers={"Content-Type": "application/jose+json"},
            )
        self._nonce = r.headers.get("Replay-Nonce", "")
        return r

    # ── ACME operations ───────────────────────────────────────────────────────

    async def ensure_account(self, contact_email: str = "") -> str:
        """Create or retrieve ACME account. Returns account URL."""
        d = await self._fetch_directory()
        payload: dict = {"termsOfServiceAgreed": True}
        if contact_email:
            payload["contact"] = [f"mailto:{contact_email}"]
        r = await self._post(d["newAccount"], payload)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"ACME new-account failed: {r.status_code} {r.text[:300]}")
        url = r.headers.get("Location", "")
        self.account_url = url
        log.info("ACME account: %s (HTTP %d)", url, r.status_code)
        return url

    async def new_order(self, email: str) -> dict:
        """Place a new S/MIME certificate order. Returns order dict + 'order_url' key."""
        d = await self._fetch_directory()
        r = await self._post(d["newOrder"], {
            "identifiers": [{"type": "email", "value": email}],
        })
        if r.status_code != 201:
            raise RuntimeError(f"ACME new-order failed: {r.status_code} {r.text[:300]}")
        order = r.json()
        order["order_url"] = r.headers.get("Location", "")
        return order

    async def new_order_dns(self, domain: str) -> dict:
        """Place a new order for a DNS identifier (TLS cert, dns-01/http-01)."""
        d = await self._fetch_directory()
        r = await self._post(d["newOrder"], {
            "identifiers": [{"type": "dns", "value": domain}],
        })
        if r.status_code != 201:
            raise RuntimeError(f"ACME new-order failed: {r.status_code} {r.text[:300]}")
        order = r.json()
        order["order_url"] = r.headers.get("Location", "")
        return order

    async def get_authorization(self, authz_url: str) -> dict:
        r = await self._post(authz_url, None)
        if r.status_code >= 400:
            log.error("ACME get_authorization failed: %s %s (%s)", r.status_code, r.text[:500], authz_url)
        r.raise_for_status()
        return r.json()

    async def trigger_challenge(self, challenge_url: str) -> dict:
        """POST to challenge URL — signals CASTLE to validate our reply."""
        r = await self._post(challenge_url, {})
        if r.status_code not in (200, 202):
            raise RuntimeError(f"ACME challenge trigger failed: {r.status_code} {r.text[:300]}")
        result = r.json()
        log.info("ACME trigger_challenge → %s", json.dumps({k: result.get(k) for k in ("status", "type", "error")}))
        return result

    async def poll_order_status(self, order_url: str, timeout_sec: int = 600) -> dict:
        """Poll until order.status ∈ {ready, valid, invalid} or timeout."""
        import asyncio
        end = asyncio.get_event_loop().time() + timeout_sec
        last_status = None
        while True:
            r = await self._post(order_url, None)
            if r.status_code >= 400:
                log.error("ACME order poll failed: %s %s (%s)", r.status_code, r.text[:500], order_url)
            r.raise_for_status()
            order = r.json()
            status = order.get("status")
            if status != last_status:
                log.info("ACME order status changed: %s → %s (%s)", last_status, status, order_url)
                last_status = status
            if status in ("ready", "valid", "invalid"):
                return order
            if asyncio.get_event_loop().time() >= end:
                raise TimeoutError(f"ACME order did not complete in {timeout_sec}s (status={status})")
            await asyncio.sleep(10)

    async def finalize(self, finalize_url: str, csr_der: bytes) -> dict:
        r = await self._post(finalize_url, {"csr": b64url(csr_der)})
        if r.status_code not in (200, 201, 202):
            raise RuntimeError(f"ACME finalize failed: {r.status_code} {r.text[:300]}")
        return r.json()

    async def download_certificate(self, cert_url: str) -> bytes:
        """Download issued cert chain (PEM)."""
        r = await self._post(cert_url, None)
        if r.status_code >= 400:
            log.error("ACME download_certificate failed: %s %s (%s)", r.status_code, r.text[:500], cert_url)
        r.raise_for_status()
        return r.content

    def _interpret_revoke_response(self, r: httpx.Response, reason: int) -> None:
        # 200 = revoked; some servers return 200 with empty body.
        # 400 alreadyRevoked is idempotent-OK (the cert is already gone — our goal).
        if r.status_code == 200:
            log.info("ACME certificate revoked (reason=%d)", reason)
            return
        body = r.text[:300]
        if r.status_code == 400 and "alreadyRevoked" in body:
            log.info("ACME certificate already revoked — treating as success")
            return
        raise RuntimeError(f"ACME revoke failed: {r.status_code} {body}")

    async def revoke_certificate(self, cert_der: bytes, reason: int = 0) -> None:
        """Revoke a certificate (RFC 8555 §7.6), signed with the ACCOUNT key.

        cert_der: the leaf certificate in DER form.
        reason: RFC 5280 CRLReason (0 = unspecified, 4 = superseded).
        Only valid if this account issued the cert (or is authorized for it).
        """
        d = await self._fetch_directory()
        if not d.get("revokeCert"):
            raise RuntimeError("ACME directory has no revokeCert endpoint")
        r = await self._post(d["revokeCert"], {"certificate": b64url(cert_der), "reason": reason})
        self._interpret_revoke_response(r, reason)

    async def revoke_with_cert_key(
        self, cert_der: bytes, cert_key: ec.EllipticCurvePrivateKey, reason: int = 0
    ) -> None:
        """Revoke a certificate signed with the CERTIFICATE'S OWN keypair (RFC 8555 §7.6).

        This works regardless of ACME account state (even if the issuing account key
        was reset/lost) because it proves possession of the cert's private key via a
        JWK-embedded JWS. Requires an EC (P-256) cert key — our ACME-issued S/MIME
        certs always are.
        """
        d = await self._fetch_directory()
        revoke_url = d.get("revokeCert")
        if not revoke_url:
            raise RuntimeError("ACME directory has no revokeCert endpoint")
        nonce = await self._get_nonce()
        header = {"alg": "ES256", "nonce": nonce, "url": revoke_url,
                  "jwk": ec_key_to_jwk(cert_key)}
        protected_b64 = b64url(json.dumps(header, separators=(",", ":")).encode())
        payload_b64 = b64url(json.dumps(
            {"certificate": b64url(cert_der), "reason": reason}, separators=(",", ":")
        ).encode())
        sig = _es256_sign(cert_key, f"{protected_b64}.{payload_b64}".encode())
        body = {"protected": protected_b64, "payload": payload_b64, "signature": b64url(sig)}
        async with httpx.AsyncClient(timeout=20, proxy=_acme_proxy()) as c:
            r = await c.post(revoke_url, json=body,
                             headers={"Content-Type": "application/jose+json"})
        self._nonce = r.headers.get("Replay-Nonce", "")
        self._interpret_revoke_response(r, reason)
