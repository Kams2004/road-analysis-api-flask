"""
Settings → Platform: CAPTCHA, SMTP e-mail, map providers, security policy.
Secrets are write-only (only `<field>_set` flags come back); every change is
audited with secrets redacted.
"""
from typing import Any

from fastapi import APIRouter, Body, Depends, Path, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, api_error, require
from app.api.openapi import err, errors
from app.schemas.auth import MessageOut
from app.db.session import get_db
from app.services import audit, map_tiles
from app.services.email_service import EmailNotConfigured, EmailSendError, send_email
from app.services.settings_service import SECTIONS, MapSettings, get_section, redact, update_section

router = APIRouter()


class TestEmailIn(BaseModel):
    """Recipient of a test e-mail."""
    to: str = Field(..., examples=["ops@example.com"])


_SECTION_LIST = ", ".join(f"`{name}`" for name in SECTIONS)


@router.get("/", response_model=dict[str, dict[str, Any]], summary="Get every settings section",
            description=f"""Every settings section ({_SECTION_LIST}) with its current values (defaults when
never saved). Secrets are never returned: each secret field is replaced by a `<field>_set` boolean;
some sections add read-only facts (`captcha.using_test_keys`, `smtp.configured`,
`cloud.redirect_uri`, `cloud.google_configured`, `google_signin.configured` …).""")
async def get_all(db: AsyncSession = Depends(get_db), _: Principal = Depends(require("settings", "view"))):
    return {name: redact(name, await get_section(db, name)) for name in SECTIONS}


@router.put("/{section}", response_model=dict[str, Any], summary="Update a settings section",
            description=f"""Merges the body into section `section` ({_SECTION_LIST}) and returns the section's
new public view (secrets as `<field>_set`). Unknown keys are ignored; omitted keys keep their value; a
secret that is absent or `null` is kept, an empty string clears it. The whole section is re-validated
(`422 invalid_settings` lists the faulty fields). Changes apply to the running API within ~10 s
(`maps` changes reset the Google tile session). Audited as `settings.update` with a redacted diff.""",
            responses=errors(err(404, "not_found", "Unknown settings section 'mapz'."),
                             err(422, "invalid_settings", "Some values are invalid.",
                                 errors=[{"field": "visibility_radius_m",
                                          "message": "Input should be greater than or equal to 100"}])))
async def put_section(request: Request,
                      section: str = Path(..., description="Section name.", examples=["mobile"]),
                      body: dict = Body(..., openapi_examples={
                          "mobile": {"summary": "Mobile app distances",
                                     "value": {"visibility_radius_m": 1500, "route_corridor_m": 80}},
                          "community": {"summary": "Community voting",
                                        "value": {"vote_max_distance_m": 300, "hide_after_absent_votes": 3}},
                          "ai_validation": {"summary": "AI validation mode",
                                            "value": {"mode": "assisted", "audit_sample_rate": 0.1,
                                                      "require_classifier_agreement": True,
                                                      "type_overrides": {"pothole": {"auto_accept": 0.9}}}},
                          "smtp": {"summary": "SMTP (secret kept when password is omitted)",
                                   "value": {"host": "smtp.gmail.com", "port": 587, "username": "noreply@example.com"}},
                      }),
                      db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(require("settings", "edit"))):
    if section not in SECTIONS:
        raise api_error(404, "not_found", f"Unknown settings section '{section}'.")
    try:
        before, after = await update_section(db, section, body, principal.label)
    except ValidationError as exc:
        raise api_error(422, "invalid_settings", "Some values are invalid.",
                        errors=[{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()])
    if section == "maps":
        map_tiles.clear_cache()
    await audit.record_now(request, "settings.update", principal=principal, object_type="settings",
                           object_id=section, details=audit.diff(before, after))
    return after


@router.post("/smtp/test", response_model=MessageOut, summary="Send a test e-mail",
             responses=errors(err(400, "smtp_not_configured", "SMTP is not configured (Settings → Platform → E-mail)."),
                              err(502, "smtp_failed", "Sending failed: (535, b'Authentication failed')")))
async def test_smtp(body: TestEmailIn, request: Request, db: AsyncSession = Depends(get_db),
                    principal: Principal = Depends(require("settings", "edit"))):
    """Sends a test message with the saved SMTP settings. Audited as `settings.smtp.test`."""
    try:
        await send_email(db, body.to, "VIASafe — test e-mail",
                         "This is a test message from the VIASafe platform. SMTP is configured correctly.")
    except EmailNotConfigured as exc:
        raise api_error(400, "smtp_not_configured", str(exc))
    except EmailSendError as exc:
        await audit.record_now(request, "settings.smtp.test", principal=principal, outcome="failure",
                               details={"to": body.to, "error": str(exc)})
        raise api_error(502, "smtp_failed", f"Sending failed: {exc}")
    await audit.record_now(request, "settings.smtp.test", principal=principal, details={"to": body.to})
    return {"message": f"Test e-mail sent to {body.to}."}


@router.post("/maps/test", response_model=MessageOut, summary="Test the Google Maps key",
             responses=errors(err(400, "google_not_configured", "Enter a Google Maps Platform API key first."),
                              err(502, "google_failed", "Map Tiles API rejected the key (403).")))
async def test_google_maps(db: AsyncSession = Depends(get_db),
                           _: Principal = Depends(require("settings", "edit"))):
    """Opens a Google Map Tiles session with the saved key (the Map Tiles API must be enabled on the
    Cloud project)."""
    cfg: MapSettings = await get_section(db, "maps")  # type: ignore[assignment]
    if not cfg.google_api_key:
        raise api_error(400, "google_not_configured", "Enter a Google Maps Platform API key first.")
    try:
        await map_tiles.google_session(cfg, force=True)
    except map_tiles.GoogleTilesError as exc:
        raise api_error(502, "google_failed", str(exc))
    return {"message": "Google Map Tiles API key works."}
