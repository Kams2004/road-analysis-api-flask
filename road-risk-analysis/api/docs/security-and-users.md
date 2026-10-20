# Security, users and roles (Module 1)

How sign-in, permissions and the related settings work, and what to configure
before going live. Requirement IDs refer to the CAMTRACK baseline v2.1.

## 1. First deployment

1. Rebuild the API image (it now needs the `cryptography` package):
   `docker-compose build api worker beat`
2. Add to `api/.env`:

   ```env
   SECRET_KEY=<long random string — openssl rand -hex 32>   # encrypts stored secrets; never change it afterwards
   WEB_BASE_URL=https://<console host>                      # used in password-reset links
   CORS_ORIGINS=https://<console host>                      # only needed for browser calls that bypass the console proxy
   AUTH_ENFORCED=true
   BOOTSTRAP_ADMIN_EMAIL=you@camtrack.net                   # first start only, when no user exists
   BOOTSTRAP_ADMIN_PASSWORD=<temporary password>
   ```

   Alternatively create the administrator by hand:
   `docker-compose exec api python -m app.cli create-admin --email you@camtrack.net --name "Your Name"`
3. Web console `.env`: `API_URL=http://<api host>:8080` and, when served over HTTPS, `COOKIE_SECURE=true`.
4. Sign in, change the bootstrap password (My Profile), then remove the `BOOTSTRAP_*` lines.
5. Create an API key for the mobile app (Administration → API Clients, or
   `python -m app.cli create-api-client --name "VIASafe mobile"`) and build the app with
   `EXPO_PUBLIC_API_KEY=<key>`. Do the same for the simulator (`API_KEY=<key>`).

Tables are created automatically on startup; `migrations/add_auth_users_roles.sql` documents the schema.

## 2. Platform settings (Settings → Platform)

All of these can be changed at any time without a restart. Secrets (CAPTCHA secret, SMTP password,
Google key) are encrypted in the database with `SECRET_KEY` and are never sent back to the browser.

| Section | What to enter |
|---|---|
| CAPTCHA | Provider (Cloudflare Turnstile by default; Google reCAPTCHA v2 and hCaptcha also supported), site key and secret key. Until real keys are entered, Cloudflare's **test keys** are used — every check passes and the page shows a warning. |
| E-mail (SMTP) | Gmail: host `smtp.gmail.com`, port `587`, encryption STARTTLS, username = the Gmail address, password = a Gmail **App Password** (Google Account → Security → 2-Step Verification → App passwords). Use "Send test e-mail" to check. Until configured, invitations and admin resets show a one-time link for the administrator to pass on; the self-service "forgot password" page cannot send anything. |
| Maps | OpenStreetMap is the free default. To offer Google Maps, enter a Google Maps Platform key with the **Map Tiles API** enabled (billing required on the Cloud project) and use "Test Google key". Each user then picks their provider in My Profile (RRAP-01-012); without a key, users who chose Google see OpenStreetMap with a notice. |
| Security policy | Session length, lockout threshold/duration, minimum password length, reset-link validity, MFA for every user. |

## 3. How access works

- **Sign-in** (RRAP-01-001/003/006): e-mail or username + password + CAPTCHA. The CAPTCHA is
  verified first, so "security check failed" and "wrong password" are reported separately.
  After the configured number of failures the account is locked for the configured time
  (an administrator can unlock it).
- **Two-step verification** (RRAP-01-002): any user can enable an authenticator app in My Profile.
  Marking a role "require two-step verification" (or the global policy) forces its members to
  enrol at their next sign-in. Administrators can reset a user's 2FA if they lose their phone.
- **Password reset** (RRAP-01-004): "Forgot your password?" e-mails a single-use link (default 30 min).
  Resetting signs out every session of that user.
- **Sessions** are server-side: logout, deactivation, password change/reset revoke them immediately.
  The console keeps the session in an httpOnly cookie; the token is never visible to page scripts.
- **Roles & permissions** (RRAP-01-007/008/009/013): each role has a View/Add/Edit/Delete matrix per
  module. A user's rights are the union of their active roles. The menu and buttons only show what
  the user may do, and the API enforces the same rules. Seeded roles: Administrator (system — always
  everything, cannot be deleted or deactivated), Validator, Operator, Analyst. A role still held by
  active users cannot be deleted. The last active Administrator cannot be removed or deactivated.
- **Users** (RRAP-01-010/011): name, e-mail, optional username, telephone, WhatsApp, roles, status,
  language (EN/FR/ES — RRAP-01-005) and map provider. New users get an initial password or an
  invitation link (valid 72 h).
- **API clients**: the mobile app, the simulator and future integrations use their own keys
  (`X-API-Key`), limited to the endpoints they need, and revocable/rotatable without affecting users.
- **Audit trail** (Administration → Audit Trail): sign-ins (success and failure), sign-outs, password
  and 2FA events, user/role/settings/API-key changes, every state-changing API call made by a user and
  the screens they open.

## 4. API

The REST API is described by an OpenAPI 3 document (RRAP-12-006 / RRAP-12-014), generated from the code
so it stays in step with the implemented endpoints:

| Where | What |
|---|---|
| `http://<api-host>:8080/docs` | Swagger UI — browse by module, **Authorize** with a session token and/or an API key, then **Try it out** (authorizations persist in the browser). |
| `http://<api-host>:8080/redoc` | ReDoc — the same document as a readable reference. |
| `http://<api-host>:8080/openapi.json` | The live machine-readable document (client generation, Postman/Insomnia import). |
| [`docs/openapi.json`](openapi.json) | Committed copy, regenerated with `python -m app.export_openapi` (a test fails when it is stale). |

The introduction of the document covers authentication (session token from `POST /auth/login` →
`Authorization: Bearer <token>`; `X-API-Key` for the mobile app and integrations; the MFA steps), the
error format `{"detail": {"code", "message", …}}` and its codes, pagination, rate limits and units.
Each operation states which credentials it accepts and the permission it needs (`**Permission:**
module:action`); both are derived from the same guards that enforce them (`app/api/deps.py`,
`app/main.py`), so the documentation cannot drift from the access rules.

## 5. Demo stack

`docker-compose.demo.yml` runs with `AUTH_ENFORCED=false` so the simulator and an unconfigured phone
keep working; the console still requires sign-in (`admin@viasafe.local` / `ChangeMe123`, created on
first start — change it).
