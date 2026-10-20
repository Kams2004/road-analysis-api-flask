"""
"Sign in with Google": verification of a Google ID token.

The token is checked by Google's tokeninfo endpoint (signature + expiry),
then locally: `aud` must be one of the configured client IDs, `iss` must be
Google, the e-mail must be verified and `exp` in the future. The HTTP client
is injectable (httpx.AsyncClient) so tests use an httpx.MockTransport.
"""
import time
from typing import Optional

import httpx

from app.services.settings_service import GoogleSigninSettings

TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


class GoogleTokenInvalid(Exception):
    """The ID token is not acceptable (bad signature, audience, expiry …)."""


class GoogleUnavailable(Exception):
    """tokeninfo could not be reached / answered unexpectedly."""


def check_claims(claims: dict, cfg: GoogleSigninSettings, now: float | None = None) -> dict:
    """Validate tokeninfo claims; returns {sub, email, name}."""
    now = time.time() if now is None else now
    if claims.get("aud") not in cfg.client_ids():
        raise GoogleTokenInvalid("audience")
    if claims.get("iss") not in _ISSUERS:
        raise GoogleTokenInvalid("issuer")
    if str(claims.get("email_verified", "")).lower() != "true":
        raise GoogleTokenInvalid("email_not_verified")
    try:
        exp = float(claims.get("exp"))
    except (TypeError, ValueError):
        raise GoogleTokenInvalid("expiry")
    if exp <= now:
        raise GoogleTokenInvalid("expired")
    sub, email = claims.get("sub"), (claims.get("email") or "").strip().lower()
    if not sub or "@" not in email:
        raise GoogleTokenInvalid("claims")
    return {"sub": str(sub), "email": email, "name": (claims.get("name") or "").strip()}


async def verify_id_token(cfg: GoogleSigninSettings, id_token: str,
                          client: Optional[httpx.AsyncClient] = None) -> dict:
    if not id_token:
        raise GoogleTokenInvalid("empty")
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        resp = await client.get(TOKENINFO_URL, params={"id_token": id_token})
    except httpx.HTTPError as exc:
        raise GoogleUnavailable(str(exc)) from exc
    finally:
        if own:
            await client.aclose()
    if resp.status_code == 400:
        raise GoogleTokenInvalid("rejected_by_google")
    if resp.status_code != 200:
        raise GoogleUnavailable(f"tokeninfo HTTP {resp.status_code}")
    try:
        claims = resp.json()
    except ValueError as exc:
        raise GoogleUnavailable("tokeninfo returned invalid JSON") from exc
    return check_claims(claims, cfg)
