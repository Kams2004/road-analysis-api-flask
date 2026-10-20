# VIASafe — Road Risk Analysis Platform

Road hazard detection (potholes, signs, speed bumps, parked vehicles), hazard validation,
connected-vehicle collision prevention and community reporting — CAMTRACK.

## Layout

```
road-risk-analysis/
  backend/   FastAPI API + Celery worker (app/), tests/, SQL migrations/, Dockerfile   [home repo]
  web/       Next.js web console                                       [own git repo]
  mobile/    Expo / React Native app (VIASafe mobile)                  [own git repo]
  ai/        model work                                                [own git repo]
    training/      pothole/, traffic-signs/ (training kits), pothole-dataset/ (Label Studio kit)
    archive/       legacy scripts, old guides, old datasets/weights (heavy dirs gitignored)
    models/        INSTALLED weights + model cards, mounted read-only on /models (gitignored)
    prototypes/    parked-vehicle-detection/                           [own git repo]
  infra/     docker-compose*.yml, start-local-test.sh, deploy/server-v2/ (server kit),
             osrm/ (Cameroon routing image + data/), secrets/, wheels/, local/   [home repo]
  docs/      specs, guides, openapi.json, requirements (docx/pdf), assets/        [home repo]
  tools/     pothole_v1/ (offline video test), archive/ (old debug scripts)        [home repo]
  data/      datasets/ (coco8 …), videos/ (test dashcam videos)                    [local only]
  logs/      web build/start, Metro, seed logs                                     [local only]
```

The home-rooted git repo (remote `road-analysis-api-flask`) tracks `backend/ infra/ docs/ tools/`,
this README and `.gitignore`; `web/`, `mobile/`, `ai/` and `ai/prototypes/parked-vehicle-detection/`
are independent repos.

## Run locally (demo/test stack `roaddemo`)

```bash
infra/start-local-test.sh            # containers + worker/beat in the API container + web on :3000
infra/start-local-test.sh --rebuild  # rebuild the web console first   (--seed: test data, --demo: simulator)
```

- API http://localhost:8080 (`/docs`), web console http://localhost:3000, MinIO :9200.
- First-time creation of the stack (from `infra/`, compose 1.29):
  `docker-compose -p roaddemo -f docker-compose.demo.yml up -d`. It mounts `backend/app`
  (live code), `ai/models` (`/models`) and `infra/secrets` (`/app/secrets`).
- Backend tests (from `backend/`): `~/.cache/viasafe-testvenv/bin/python -m pytest -q`.
  `python -m app.export_openapi` rewrites `docs/openapi.json` (a test checks it is current).
- Mobile (from `mobile/`): `npx expo start --dev-client --port 8081 --localhost`.

## Deploy (server v2 stack, separate from production)

```bash
cd infra/deploy/server-v2 && ./push.sh          # PC → ~/viasafe-v2/{backend,web,infra,ai/models,docs}
ssh … 'cd ~/viasafe-v2/infra/deploy/server-v2 && ./deploy.sh'   # build + start (project viasafe-v2)
```

See `docs/TEST-PHASE.md`. Upgrading a server that still has `~/viasafe-v2/api`:
`docs/MIGRATION-LAYOUT.md`. The legacy production stack uses `infra/docker-compose.yml`
(+ `infra/.env`, `infra/setup-server.sh`).

## Secrets (never in git, images or the UI)

| What | Dev PC | Server (v2) |
|------|--------|-------------|
| SFTP passwords / keys | `infra/secrets/sftp.env` (chmod 600) | `~/viasafe-v2/infra/secrets/sftp.env` |
| v2 stack passwords (generated) | — | `~/viasafe-v2/infra/deploy/server-v2/.env` |
| Legacy stack settings | `infra/.env` | `.env` next to its `docker-compose.yml` |
| Web console tester password (seed) | `infra/local/web-test-credentials.txt` | `TESTER_PASSWORD` in the v2 `.env` |
| Web / mobile env | `web/.env.local`, `mobile/.env*` | build args in `docker-compose.v2.yml` |

Templates: `infra/secrets/sftp.env.example`; details in `infra/secrets/README.md` and
`docs/SFTP-SOURCE.md`. Installing new weights: copy the `.pt` + `.json` card into `ai/models/`
(`docs/models-pothole.md`).
