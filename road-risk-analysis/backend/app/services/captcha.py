"""
Server-side verification of the online CAPTCHA token produced by the login
page widget (RRAP-01-003). Cloudflare Turnstile, Google reCAPTCHA v2 and
hCaptcha share the same siteverify contract: form-POST secret + response,
JSON reply with a boolean `success`.
"""
import logging

import httpx

from app.services.settings_service import CaptchaSettings

logger = logging.getLogger(__name__)

VERIFY_URLS = {
    "turnstile": "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    "recaptcha": "https://www.google.com/recaptcha/api/siteverify",
    "hcaptcha":  "https://api.hcaptcha.com/siteverify",
}


class CaptchaUnavailable(Exception):
    """The provider could not be reached — distinct from a failed challenge."""


async def verify_captcha(cfg: CaptchaSettings, token: str | None, remote_ip: str | None) -> bool:
    if cfg.provider == "disabled":
        return True
    if not token:
        return False
    data = {"secret": cfg.secret_key, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.post(VERIFY_URLS[cfg.provider], data=data)
        body = res.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("CAPTCHA verification (%s) unreachable: %s", cfg.provider, exc)
        raise CaptchaUnavailable(str(exc)) from exc
    if not body.get("success"):
        logger.info("CAPTCHA rejected (%s): %s", cfg.provider, body.get("error-codes"))
    return bool(body.get("success"))
