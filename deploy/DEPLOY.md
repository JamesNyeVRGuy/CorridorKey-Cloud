# CorridorKey Cloud Deployment Guide

## Quick Start

```bash
cd deploy

# 1. Create your .env from the example
cp .env.example .env

# 2. Generate secrets and paste them into .env
openssl rand -hex 32   # → POSTGRES_PASSWORD
openssl rand -hex 32   # → JWT_SECRET, CK_JWT_SECRET, GOTRUE_JWT_SECRET (all same value)

# 3. Set CK_AUTH_ENABLED=true in .env

# 4. Start the stack
docker compose -f docker-compose.dev.yml --env-file .env up -d --build

# 5. Create the first admin user
./create-admin.sh
```

## Environment Configuration

Everything lives in a single `.env` file. Key rules:

- **No quotes** around values: `JWT_SECRET=abc123` not `JWT_SECRET="abc123"`
- **No trailing spaces** after values
- **Unix line endings** (LF). Windows editors may save as CRLF which breaks env var parsing.
  Check with: `file .env` — should say "ASCII text", not "ASCII text, with CRLF line terminators"
  Fix with: `dos2unix .env` or `sed -i 's/\r$//' .env`
- **Three secrets must match**: `JWT_SECRET`, `CK_JWT_SECRET`, and `GOTRUE_JWT_SECRET` must all be the same value. GoTrue signs JWTs with it, CorridorKey validates with it.

### Required Variables (for auth mode)

| Variable | Description |
|----------|-------------|
| `POSTGRES_PASSWORD` | Database password (all Supabase services use this) |
| `JWT_SECRET` | JWT signing secret (shared between GoTrue and CK) |
| `CK_JWT_SECRET` | Same as JWT_SECRET (CK reads this name) |
| `CK_AUTH_ENABLED` | `true` to enable login/auth |
| `CK_GOTRUE_INTERNAL_URL` | `http://supabase-auth:9999` (Docker internal) |
| `CK_DATABASE_URL` | `postgresql://supabase_admin:<pw>@supabase-db:5432/corridorkey` |
| `CK_MIGRATION_URL` | `postgresql://supabase_admin:<pw>@supabase-db:5432/corridorkey` |
| `SERVICE_ROLE_KEY` | Supabase admin API key (for create-admin.sh) |

### Optional Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CK_METRICS_ENABLED` | `false` | Enable Prometheus /metrics endpoint |
| `CK_LOG_FORMAT` | `text` | `json` for Loki/structured logging |
| `CK_DOCS_PUBLIC` | auto | `true`/`false` to override API docs access |
| `CK_STORAGE_BACKEND` | `local` | `s3` for S3-compatible storage |

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Docker Network                                      │
│                                                       │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────┐ │
│  │  supabase-db │  │ supabase-auth│  │ corridorkey │ │
│  │  (Postgres)  │←─│   (GoTrue)   │  │    -web     │ │
│  │  :5432       │  │  :9999       │←─│  :3000      │ │
│  └──────────────┘  └──────────────┘  └──────┬──────┘ │
│                                              │        │
└──────────────────────────────────────────────┼────────┘
                                               │
                                          Port 3000
                                          (or via Caddy)
```

- **supabase-db**: Postgres 15 with Supabase extensions. Stores auth users and CK app state.
- **supabase-auth**: GoTrue — handles login, signup, JWT issuance.
- **corridorkey-web**: FastAPI server + Svelte SPA. All browser traffic goes here.

## Docker Compose Files

| File | Purpose |
|------|---------|
| `docker-compose.dev.yml` | Full stack: CK + Supabase (builds from source) |
| `docker-compose.web.yml` | CK web server only (uses pre-built image) |
| `docker-compose.node.yml` | Render farm node agent |
| `docker-compose.node-hardened.yml` | Hardened node (read-only, dropped caps) |
| `docker-compose.monitoring.yml` | Prometheus + Grafana + Loki |
| `docker-compose.caddy.yml` | TLS/HTTPS via Caddy |
| `docker-compose.supabase.yml` | Supabase stack only (no CK) |

Compose files are **composable**:
```bash
# Dev + monitoring
docker compose -f docker-compose.dev.yml -f docker-compose.monitoring.yml --env-file .env up -d

# Production: pre-built image + Caddy HTTPS
docker compose -f docker-compose.web.yml -f docker-compose.caddy.yml --env-file .env up -d
```

## First-Time Setup

### 1. Generate Supabase API Keys

You need ANON_KEY and SERVICE_ROLE_KEY. Generate them using the JWT_SECRET:

```bash
# Using the Supabase key generator:
# https://supabase.com/docs/guides/self-hosting#api-keys

# Or manually with jwt-cli:
# ANON_KEY: role=anon
# SERVICE_ROLE_KEY: role=service_role
```

### 2. Create Admin User

After the stack is up:
```bash
./create-admin.sh
```

This creates the first `platform_admin` user via GoTrue's admin API, bypassing the `DISABLE_SIGNUP=true` restriction.

### 3. Invite Users

1. Log in as admin at `http://localhost:3000/login`
2. Go to Admin → Users → Generate Invite Link
3. Share the link with your team
4. Approve users in the Admin → Users → Pending section

## Docker Swarm Deployment

The `prod-up.sh` and `prod-down.sh` scripts use `docker compose`, not Swarm.
Swarm uses `docker stack deploy` which handles env vars, volumes, and
networking differently. This section covers the gotchas.

### Deploying to Swarm

```bash
# 1. Prepare compose for Swarm
#    Swarm doesn't support --env-file on docker stack deploy.
#    Add env_file: to each service in your compose file instead.

# 2. Pull images first (Swarm doesn't auto-pull)
docker pull supabase/postgres:15.6.1.143
docker pull supabase/gotrue:v2.170.0
docker pull ghcr.io/jamesnyevrguy/corridorkey-web:cloud

# 3. Deploy
docker stack deploy -c docker-compose.dev.yml corridorkey
```

### Swarm Compose Modifications

You'll need to modify the compose files for Swarm compatibility.
Key changes:

#### 1. Use `env_file:` instead of `--env-file`

`--env-file` on the CLI does variable **substitution** in the compose
file at parse time. `env_file:` inside a service loads variables
**directly into the container** at runtime. Swarm needs the latter:

```yaml
services:
  corridorkey-web:
    env_file:
      - .env                    # loads ALL vars into container
    environment:
      CK_CLIPS_DIR: /app/Projects  # overrides/additions
```

#### 2. Use mapping format for `environment:`

```yaml
# ✅ Works in Swarm — mapping format
environment:
  CK_AUTH_ENABLED: ${CK_AUTH_ENABLED}
  GOTRUE_JWT_SECRET: ${JWT_SECRET}

# ❌ Can break in Swarm — list format with defaults
environment:
  - CK_AUTH_ENABLED=${CK_AUTH_ENABLED:-false}
  - GOTRUE_JWT_SECRET=${JWT_SECRET:?Set JWT_SECRET}
```

The list format with `:-` defaults and `:?` error syntax doesn't
always work in Swarm's variable substitution.

#### 3. Remove `build:` directives

Swarm doesn't build images — it only pulls. Replace `build:` with
`image:` pointing to a pre-built image:

```yaml
# ❌ Swarm can't build
build:
  context: ..
  dockerfile: web/Dockerfile.web

# ✅ Use pre-built image
image: ghcr.io/jamesnyevrguy/corridorkey-web:cloud
```

Build and push the image separately:
```bash
docker build -t ghcr.io/jamesnyevrguy/corridorkey-web:cloud -f web/Dockerfile.web .
docker push ghcr.io/jamesnyevrguy/corridorkey-web:cloud
```

#### 4. Remove `depends_on:` with `condition:`

Swarm ignores `depends_on` conditions. Services start in parallel.
Use healthchecks and restart policies instead — services will retry
until dependencies are ready.

### Database Volumes in Swarm

**Use local volumes for Postgres, not NFS.** Databases on NFS have
fsync issues, stale file handles, and lock contention that cause
init scripts to fail or data corruption.

```yaml
volumes:
  supabase-db-data:
    driver: local   # local disk, NOT NFS
```

NFS is fine for project files (`CK_PROJECTS_DIR`) and model weights.

**Swarm volumes survive `docker stack rm`.** To truly reset the DB:

```bash
docker stack rm corridorkey
sleep 10                          # wait for containers to die
docker volume ls | grep db        # find the volume
docker volume rm corridorkey_supabase-db-data
docker stack deploy ...           # redeploy — init scripts will run
```

The `sleep` matters — if the Postgres container hasn't fully stopped,
it recreates the data directory before your `rm` takes effect.

**Init scripts only run on a fresh volume.** If you see
`PostgreSQL Database directory appears to contain a database; Skipping initialization`,
the volume has stale data. Nuke it.

### Troubleshooting: Role Passwords Not Set

If GoTrue fails with `password authentication failed for supabase_auth_admin`,
the `POSTGRES_PASSWORD` env var didn't reach the DB container during init.
This is common in Swarm because env delivery is less predictable.

**Quick fix** — set passwords manually after init:

```bash
docker exec $(docker ps -q -f name=supabase-db) \
  psql -U supabase_admin -d corridorkey -c "
    ALTER ROLE supabase_auth_admin WITH PASSWORD 'YOUR_POSTGRES_PASSWORD';
    ALTER ROLE authenticator WITH PASSWORD 'YOUR_POSTGRES_PASSWORD';
  "
```

Then restart GoTrue:
```bash
docker service update --force corridorkey_supabase-auth
```

**Permanent fix** — ensure `POSTGRES_PASSWORD` is in `env_file:` for
the `supabase-db` service, not just in the `environment:` mapping.

### Troubleshooting: Images Not Found After Prune

`docker system prune` removes unused images. Swarm's `docker stack deploy`
doesn't auto-pull — it expects images to be local. After a prune:

```bash
# Re-pull all images before redeploying
docker pull supabase/postgres:15.6.1.143
docker pull supabase/gotrue:v2.170.0
docker pull postgrest/postgrest:v12.2.3
docker pull ghcr.io/jamesnyevrguy/corridorkey-web:cloud
```

### Example: Minimal Swarm-Compatible Compose

```yaml
# docker-compose.swarm.yml — adapted for docker stack deploy
version: "3.8"

services:
  supabase-db:
    image: supabase/postgres:15.6.1.143
    env_file: [.env]
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: corridorkey
    volumes:
      - supabase-db-data:/var/lib/postgresql/data
      - ./init-db.sql:/docker-entrypoint-initdb.d/99-corridorkey.sql:ro
    deploy:
      restart_policy:
        condition: any

  supabase-auth:
    image: supabase/gotrue:v2.170.0
    env_file: [.env]
    environment:
      GOTRUE_API_HOST: "0.0.0.0"
      GOTRUE_API_PORT: "9999"
      GOTRUE_DB_DRIVER: postgres
      GOTRUE_DB_DATABASE_URL: postgres://supabase_auth_admin:${POSTGRES_PASSWORD}@supabase-db:5432/corridorkey
      GOTRUE_JWT_SECRET: ${JWT_SECRET}
      GOTRUE_JWT_AUD: authenticated
      GOTRUE_JWT_DEFAULT_GROUP_NAME: authenticated
      GOTRUE_DISABLE_SIGNUP: "true"
      GOTRUE_MAILER_AUTOCONFIRM: "true"
      GOTRUE_SITE_URL: ${SITE_URL:-http://localhost:3000}
    deploy:
      restart_policy:
        condition: any

  corridorkey-web:
    image: ghcr.io/jamesnyevrguy/corridorkey-web:cloud
    env_file: [.env]
    environment:
      OPENCV_IO_ENABLE_OPENEXR: "1"
      CK_CLIPS_DIR: /app/Projects
      CK_GOTRUE_INTERNAL_URL: http://supabase-auth:9999
      CK_DATABASE_URL: postgresql://postgres:${POSTGRES_PASSWORD}@supabase-db:5432/corridorkey
      CK_MIGRATION_URL: postgresql://supabase_admin:${POSTGRES_PASSWORD}@supabase-db:5432/corridorkey
    ports:
      - "3000:3000"
    volumes:
      - projects:/app/Projects
    deploy:
      restart_policy:
        condition: any
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

volumes:
  supabase-db-data:
    driver: local
  projects:
```

## Monitoring Setup

### With the bundled stack

```bash
docker compose -f docker-compose.dev.yml -f docker-compose.monitoring.yml --env-file .env up -d
```

Set `CK_METRICS_ENABLED=true` in `.env`. Access:
- Grafana: `http://localhost:3001` (admin/admin — change in `.env`)
- Prometheus: `http://localhost:9090`

### With an external Prometheus

Add to your existing `prometheus.yml`:
```yaml
- job_name: corridorkey
  metrics_path: /metrics
  scrape_interval: 15s
  static_configs:
    - targets: ['YOUR_CK_IP:3000']
```

Import dashboards from `deploy/monitoring/grafana/dashboards/` into your Grafana.

## Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| 404 on `/login` | `CK_AUTH_ENABLED` not reaching container | Check for CRLF line endings in `.env`. Run `dos2unix .env` |
| `auth_enabled: false` but env is set | Trailing `\r` in env value | Same: `dos2unix .env` |
| GoTrue: `password authentication failed` | DB volume has stale data | Nuke volume and redeploy |
| GoTrue: `role does not exist` | Not using `supabase/postgres` image | Must use `supabase/postgres:15.6.1.143`, not `postgres:15` |
| `Skipping initialization` | Volume already exists | `docker volume rm <volume>` |
| `no such image` after prune | Swarm doesn't auto-pull | `docker pull <image>` before deploy |
