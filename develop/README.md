# CorridorKey development stack

This stack runs the Next.js frontend, Python API, local Supabase-compatible
services, Redis, and observability services with ordinary Docker Compose. It
does not use production secrets, NFS exports, Swarm placement, Traefik, or
remote render-node credentials.

## Start

```bash
cd develop
cp .env.example .env
docker compose --env-file .env -f compose.yml config --quiet
docker compose --env-file .env -f compose.yml up -d --build
docker compose --env-file .env -f compose.yml ps
docker compose --env-file .env -f compose.yml logs -f frontend corridorkey-web
```

When authentication is enabled, set the admin email in `.env` before starting
the stack:

```env
CK_ADMIN_EMAIL=admin@example.com
```

Run the one-shot bootstrap profile interactively. It prompts for the password
twice and does not store it in `.env`:

```bash
docker compose --env-file .env -f compose.yml --profile admin run --rm admin-bootstrap
```

The service creates or updates the user as a `platform_admin` through GoTrue.
For a generated password, use this command and paste the result into the
prompt:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
```

The bootstrap derives a short-lived local `service_role` JWT from `JWT_SECRET`
when no explicit service key is supplied. You may set `SERVICE_ROLE_KEY` or
`CK_SUPABASE_SERVICE_KEY` instead; both are server-only and must never be
exposed to the browser. A one-way password hash cannot be passed to GoTrue's
admin API; GoTrue must receive the password over the local container network
and stores its own password hash.

The browser uses the React/Next.js frontend at `http://localhost:3000`.
Next.js rewrites `/api/*` and `/ws` to the Python service at
`corridorkey-web:3000` over the private Compose network. The Python service
is also available as an API-only host endpoint at `http://localhost:3001`; it
does not serve the legacy Svelte frontend in this development topology.
`CK_SERVE_LEGACY_FRONTEND=false` explicitly disables that fallback.

`JWT_SECRET` and `CK_JWT_SECRET` must contain the same value. GoTrue uses
`JWT_SECRET` to sign login tokens, while the Python API reads `CK_JWT_SECRET`
to validate them. Compose wires both values from `JWT_SECRET` by default.

`CK_AUTH_ENABLED` is the single authentication switch for this local stack.
Compose passes it to GoTrue's email/password provider internally, so setting
`CK_AUTH_ENABLED=false` disables both API authentication and email login.
GoTrue then returns `422: Email logins are disabled` from `/token`, even when
the user already exists.

The Next.js frontend allows upload request bodies up to 10 GB, matching the
default `CK_MAX_UPLOAD_MB=10240` backend limit. This prevents Next.js from
truncating large uploads at its default 10 MB middleware buffer.

The backend healthcheck uses the Python interpreter inside the image rather
than `curl`, because the backend image does not install curl. It checks the
fast `/api/health` path after a 30-second startup grace period.

## Local URLs

| Service | URL |
|---|---|
| React frontend | http://localhost:3000 |
| Backend API (API-only host port) | http://localhost:3001 |
| GoTrue | http://localhost:54324 |
| PostgREST | http://localhost:54321 |
| Supabase Studio | http://localhost:54323 (profile `studio`) |
| Grafana | http://localhost:3002 |
| Prometheus | http://localhost:9090 |
| Mailpit | http://localhost:8025 (profile `mail`) |

Promtail is opt-in because Docker Desktop on macOS may not expose Linux
container log paths. Enable it with `--profile observability` after confirming
the configured Docker log mount works on the host:

```bash
docker compose --env-file .env -f compose.yml --profile observability up -d
```

Studio and mail are also optional:

```bash
docker compose --env-file .env -f compose.yml --profile studio --profile mail up -d
```

The default backend image is CPU-compatible and does not install the CUDA
PyTorch extra. For GPU inference on a supported x86_64/NVIDIA host, set
`CK_INSTALL_CUDA=true` in `.env`, install a compatible NVIDIA Container
Toolkit, configure `NVIDIA_VISIBLE_DEVICES`, and rebuild:

```bash
CK_INSTALL_CUDA=true docker compose --env-file .env -f compose.yml build corridorkey-web
```

CPU-only startup validates auth, metadata, uploads, API contracts, and UI
states but cannot prove inference performance or GPU output correctness. The
locked CUDA wheel is not available for ARM64 hosts.

## Stop and reset

```bash
docker compose --env-file .env -f compose.yml down
```

The following command removes all development database, Redis, metrics, logs,
and Grafana state:

```bash
docker compose --env-file .env -f compose.yml down -v
```

Do not run the volume-reset command against a production Compose project.
The database initialization scripts run only when the database volume is empty.
The password script is intentionally named `z_98-init-passwords.sh` inside the
container so it runs after the Supabase image creates its service roles.
After changing `POSTGRES_PASSWORD`, or when GoTrue reports password
authentication failures for `supabase_auth_admin`, reset the development
volume and start the stack again.
