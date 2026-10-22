# Security and load — what protects the platform

## Who talks to what

```
browser ──▶ web console (Next.js, :13000)  ──server-side proxy /api/proxy──▶ API (:18080) ──▶ Postgres, Redis, MinIO,
                 httpOnly session cookie                                                     OSRM, SFTP, Google Drive,
                                                                                             Label Studio, Nominatim
mobile app ──▶ API (:18080) + public images (MinIO :19200, buckets detections / signalements, read-only)
annotator  ──▶ Label Studio (:18085) ──images──▶ API /labelstudio/image/… (HMAC-signed URLs only)
```

* The browser never connects to SFTP, Google Drive, MinIO, Postgres, OSRM or Nominatim: every call goes through
  the web console's server-side proxy to the API, which holds the credentials. Leaflet's styles and icons are served
  by the console (`public/vendor/leaflet`); only the map tiles (OpenStreetMap) and the optional captcha script come
  from outside.
* Secrets never reach the browser: the session token is an `httpOnly` cookie, SFTP passwords live only in
  `infra/secrets/*.env` (chmod 600, never committed or pushed), stored integration secrets (SMTP, Google /
  Microsoft, captcha) are encrypted in the database and returned only as `*_set` flags.

## Web console (`web/`)

| Protection | Where |
|---|---|
| Session in an `httpOnly`, `SameSite=Lax` cookie (`COOKIE_SECURE=true` behind HTTPS) | `lib/session.ts` |
| Login required for every page and the proxies | `middleware.ts` |
| Cross-site writes refused (`Origin` ≠ host → 403), `..` path segments refused | `app/api/proxy/[...path]/route.ts` |
| Image proxy limited to the `detections` / `signalements` buckets, safe path segments only | `app/api/media/[...path]/route.ts` |
| `X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`, `nosniff`, Referrer / Permissions policies, no `X-Powered-By` | `next.config.mjs` |

## API (`backend/`)

| Protection | Where |
|---|---|
| SQL injection: SQLAlchemy expressions everywhere; the few raw SQL queries use bound parameters (`:lat`); the start-up DDL is constant text; dynamic column names come from fixed lists | `app/core/spatial.py`, `app/core/db/upgrade.py` |
| Permission guard per router (module × action), API keys limited to the mobile endpoints | `app/api/router.py`, `app/api/deps.py` |
| Login: captcha, account lockout, MFA; per-IP limit on sign-in / sign-up / password reset / MFA (`RATE_LIMIT_AUTH_PER_MIN`, 30) and on the Label Studio images (`RATE_LIMIT_PUBLIC_PER_MIN`, 600) → `429` | `app/core/rate_limit.py` |
| Client IP: `X-Forwarded-For` believed only from the internal network (web proxy) — a direct caller cannot forge it | `app/core/audit.py` (`client_ip`) |
| Page sizes capped (`limit ≤ 1000`, `skip ≥ 0`) | list routes |
| `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` on every response | `app/main.py` |
| Everything state-changing by a user is audited | `app/main.py` (`audit_mutations`) |

## Load

* **API processes** — `API_WORKERS` (server `.env`, default 2) uvicorn workers. The start-up work (schema upgrade,
  seeds, back-fills) is serialised by a Postgres advisory lock; the geocoding back-fill runs in one process only.
  Settings are cached per process; saving one clears it in every API and video-worker process at once
  (Redis pub/sub, `app/core/cache_bus.py`: settings sections, cluster rules, collision thresholds, map tiles).
  The 10 s TTL remains as the safety net, and a process that lost Redis clears all its caches on reconnect.
* **Database pool** — per process `DB_POOL_SIZE` (5) + `DB_MAX_OVERFLOW` (10), `DB_POOL_TIMEOUT_S` 30,
  `DB_POOL_RECYCLE_S` 1800. Keep `(API_WORKERS + Celery processes) × 15` under Postgres `max_connections` (100).
* **Indexes** for the busiest filters (detections per job / newest first, map bounding boxes on detections, hazards,
  signalements, historical events) — `SCALE_INDEX_STATEMENTS` in `app/core/db/upgrade.py`; existing indexes and
  columns are skipped at start-up, so a restart never locks a table.
* **Video processing** — Celery `WORKER_CONCURRENCY` (server `.env`); jobs queue in Redis, nothing is lost when all
  workers are busy.

## Known limits

* Behind the web console the client IP comes from the browser's `X-Forwarded-For` when there is no reverse proxy in
  front: the per-IP limit can be dodged through the console, the per-account lockout and the captcha still apply.
  Put nginx / Caddy with TLS in front (sets `X-Forwarded-For`, `COOKIE_SECURE=true`) for production.
* The API docs (`/docs`) are public (the endpoints themselves need a session or an API key).
* Plain HTTP on the server: credentials travel unencrypted until TLS is added in front.
