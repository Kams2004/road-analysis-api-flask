# HTTPS for the server stack (infra/deploy/server-v2)

Optional, driven by one line of the server's `.env`. Without them the stack is served over plain HTTP
on its ports (13000 web, 18080 API, 19200 media, 18085 Label Studio), as before.

```
HTTPS_DOMAIN=viasafe.example.com

```

`./deploy.sh` then also starts the `caddy` service (profile `https`, ports 80 + 443), which obtains and
renews the Let's Encrypt certificates by itself and redirects HTTP to HTTPS:

| Address | Serves |
|---|---|
| `https://<HTTPS_DOMAIN>` | web console |
| `https://api.<HTTPS_DOMAIN>` | API (mobile app, Label Studio images, `/docs`) |
| `https://media.<HTTPS_DOMAIN>` | public images of the mobile app (MinIO) |
| `https://labels.<HTTPS_DOMAIN>` | Label Studio |

and switches the rest to these addresses: the console's session cookie becomes `Secure`, the web image is
rebuilt with the HTTPS API address, Label Studio's public address and the image links it loads
(`infra/secrets/labelstudio.env`) follow.

## Before enabling it

1. DNS: four **A records** to the server's IP — `<domain>`, `api.<domain>`, `media.<domain>`,
   `labels.<domain>` (check: `dig +short api.<domain>`).
2. Ports 80 and 443 reachable from the internet (Let's Encrypt validates on port 80). Docker publishes them
   itself; a provider-side firewall (control panel) must allow them.
3. Nothing else listening on 80 / 443 on the server (`deploy.sh` checks).

Then add the line to `.env` and run `./deploy.sh`. The first certificate takes a few seconds;
`./deploy.sh --logs caddy` shows the issuance.

The plain-HTTP ports stay open meanwhile; once everything works over HTTPS (and the mobile app is built
with `EXPO_PUBLIC_API_URL=https://api.<domain>`, `EXPO_PUBLIC_MINIO_URL=https://media.<domain>`), they can
be closed by binding them to 127.0.0.1 in `docker-compose.v2.yml`.

## Google / Microsoft drive sign-in

The OAuth return address (Settings → Integrations & data shows it) is `<console>/settings/cloud/callback`.
Microsoft and Google accept plain `http` only for `localhost`, so:

* **with HTTPS** — `https://<HTTPS_DOMAIN>/settings/cloud/callback`; register that address.
* **without HTTPS** — `http://localhost:3000/settings/cloud/callback`; register that address and connect each
  drive once through a tunnel from your PC: `ssh -N -L 3000:localhost:13000 <user>@<server>`, then open
  http://localhost:3000 and add the source. The server keeps the access afterwards (no tunnel needed).

E-mail links (password reset) and report links always use the public console address (`WEB_BASE_URL`,
set by `deploy.sh`).

