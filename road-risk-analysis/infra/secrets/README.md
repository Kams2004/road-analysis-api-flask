# infra/secrets — credentials that never go to git, the database or the UI

| File | Used by | Content |
|------|---------|---------|
| `sftp.env` | API + worker (SFTP video sources) | `SFTP_PASSWORD_<KEY>=…` per SFTP server |
| `*.key`, `*_ed25519`, … (optional) | same | private keys named by `SFTP_KEYFILE_<KEY>=` |

- Create it from the template: `cp sftp.env.example sftp.env && chmod 600 sftp.env`.
- `<KEY>` is the **password key** typed when adding the server in the web console
  (*Cloud sources → Add a source → SFTP server*), e.g. `CAMTRACK` → `SFTP_PASSWORD_CAMTRACK=…`.
- The file is read at each connection: no restart is needed after editing it.
- Ignored by git (`.gitignore`) and Docker builds (`.dockerignore`), never copied by
  `infra/deploy/server-v2/push.sh`: create it **on each machine** yourself.
- Mounted read-only at `/app/secrets` in the `api` and `worker` containers.

Locations:

| Machine | Path |
|---------|------|
| Dev PC | `road-risk-analysis/infra/secrets/sftp.env` |
| Server (v2 stack) | `~/viasafe-v2/infra/secrets/sftp.env` (`chmod 600`) |

See `docs/SFTP-SOURCE.md` (project-level `docs/`).
