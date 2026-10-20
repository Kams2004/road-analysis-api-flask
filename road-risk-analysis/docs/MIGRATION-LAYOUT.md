# Server migration: `~/viasafe-v2/api` → modular layout (2026-10-20)

The project was split into top-level folders (`backend/ web/ mobile/ ai/ infra/ docs/ tools/`).
The v2 server stack follows the same layout:

| Before | After |
|--------|-------|
| `~/viasafe-v2/api/` (app, Dockerfile, …) | `~/viasafe-v2/backend/` |
| `~/viasafe-v2/api/road-risk-analysis-interface/` | `~/viasafe-v2/web/` |
| `~/viasafe-v2/api/deploy/server-v2/` (+ its `.env`) | `~/viasafe-v2/infra/deploy/server-v2/` |
| `~/viasafe-v2/api/osrm/` | `~/viasafe-v2/infra/osrm/` |
| `~/viasafe-v2/api/secrets/sftp.env` | `~/viasafe-v2/infra/secrets/sftp.env` |
| `~/viasafe-v2/api/models/` | `~/viasafe-v2/ai/models/` |
| `~/viasafe-v2/api/docs/` | `~/viasafe-v2/docs/` |

The Docker **project name stays `viasafe-v2`** (`name:` in `docker-compose.v2.yml` and `-p` in
`deploy.sh`), so the volumes `viasafe-v2_db_data`, `viasafe-v2_minio_data`, `viasafe-v2_osrm_data`
and `viasafe-v2_uploads` — all the data — are reused as they are. Only the containers are recreated.

The one thing that must not be lost is `deploy/server-v2/.env`: it holds the generated DB / MinIO
passwords the existing volumes were initialised with. `deploy.sh` refuses to run in the new place
while that file is still only in the old one.

## Steps

1. **Server** — prepare (the old stack keeps running):
   ```bash
   mkdir -p ~/viasafe-v2/infra/deploy/server-v2 ~/viasafe-v2/infra/secrets ~/viasafe-v2/ai
   cp -a ~/viasafe-v2/api/models ~/viasafe-v2/ai/models      # saves re-uploading the weights
   ```
2. **PC** — push the new layout:
   ```bash
   cd ~/road-risk-analysis/infra/deploy/server-v2 && ./push.sh
   ```
3. **Server** — move the settings and the secrets (keep mode 600, never print them):
   ```bash
   mv ~/viasafe-v2/api/deploy/server-v2/.env ~/viasafe-v2/infra/deploy/server-v2/.env
   chmod 600 ~/viasafe-v2/infra/deploy/server-v2/.env
   # SFTP passwords and key files, if any
   for f in ~/viasafe-v2/api/secrets/*.env ~/viasafe-v2/api/secrets/*.key \
            ~/viasafe-v2/api/secrets/*_ed25519 ~/viasafe-v2/api/secrets/*_rsa ~/viasafe-v2/api/secrets/*_ecdsa; do
     [ -e "$f" ] && mv "$f" ~/viasafe-v2/infra/secrets/ && chmod 600 ~/viasafe-v2/infra/secrets/"$(basename "$f")"
   done
   ls -l ~/viasafe-v2/infra/secrets
   ```
4. **Server** — redeploy from the new place (same mode as before: add `--with-demo` if the
   simulator / demo data were in use):
   ```bash
   cd ~/viasafe-v2/infra/deploy/server-v2 && ./deploy.sh
   ```
   Images are rebuilt from `../../../backend` and `../../../web`; the containers
   `viasafe-v2-*` are recreated with the new mounts (`~/viasafe-v2/ai/models` → `/models`,
   `~/viasafe-v2/infra/secrets` → `/app/secrets`).
5. **Check**:
   ```bash
   ./deploy.sh --status
   docker volume ls | grep viasafe-v2                     # same four volumes as before
   docker inspect viasafe-v2-api-1 --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
   curl -sf http://127.0.0.1:$(grep ^API_PORT .env | cut -d= -f2)/health
   ```
   Sign in on the web console with the same admin account; the detections, jobs and cloud
   sources are still there. *Cloud sources* → an SFTP card must show *Password: Set*.
6. **Clean up** once everything works:
   ```bash
   rm -rf ~/viasafe-v2/api
   docker image prune -f          # dangling layers of the previous build
   ```

Rollback (before step 6): `mv ~/viasafe-v2/infra/deploy/server-v2/.env ~/viasafe-v2/api/deploy/server-v2/.env`
(and the secrets back to `~/viasafe-v2/api/secrets/`), then `cd ~/viasafe-v2/api/deploy/server-v2 && ./deploy.sh`.
