# Local infrastructure (docker-compose) — runbook

Local dev infra for IOSForge (SPEC §8): **PostgreSQL 16**, **Redis 7**, **MinIO** (S3-compatible).
Defined in `/opt/IOSForge/docker-compose.yml`. Task: T-1.2.

## Prerequisites
- Docker 29.x with `docker compose` v2+ (daemon running).
- Optional: copy env defaults — `cp .env.example .env` and adjust. The stack also boots
  using built-in DEV defaults if no `.env` is present.

## Services & ports

| Service  | Image                | Host port(s)        | Purpose                          |
|----------|----------------------|---------------------|----------------------------------|
| postgres | `postgres:16`        | `5432`              | Job/state/audit DB               |
| redis    | `redis:7`            | `6379`              | Celery broker / result backend   |
| minio    | `minio/minio:latest` | `9000` (S3 API), `9001` (console) | Artifact storage |
| minio-init | `minio/mc:latest`  | —                   | One-shot: creates artifacts bucket + enables versioning |

Volumes (named, persistent): `postgres-data`, `redis-data`, `minio-data`.
Network: default compose bridge (`iosforge_default`).

## Environment variables
Read from `.env` with safe DEV defaults (`${VAR:-default}` in compose). Keep
`POSTGRES_*` in sync with `DATABASE_URL`. The S3 access/secret keys double as the
MinIO root credentials. Never commit real secrets — `.env` is gitignored.

Relevant keys: `POSTGRES_USER/PASSWORD/DB/PORT`, `REDIS_PORT`,
`S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET`, `MINIO_API_PORT`, `MINIO_CONSOLE_PORT`.

## Commands

```bash
# Start everything (detached). minio-init runs once and exits 0.
docker compose up -d

# Status + health (wait until postgres/redis/minio show "healthy")
docker compose ps

# Logs
docker compose logs -f                 # all
docker compose logs minio-init         # bucket creation result

# Stop (keep data)
docker compose down

# Stop AND wipe data volumes (destructive — local only)
docker compose down -v

# Re-create the artifacts bucket on demand (idempotent)
docker compose up minio-init
```

## MinIO console
Open http://localhost:9001 — log in with `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY`
(defaults `iosforge-dev` / `iosforge-dev-secret`). S3 API endpoint: http://localhost:9000.

## Health / connectivity checks

```bash
docker compose exec -T redis redis-cli ping            # -> PONG
docker compose exec -T postgres pg_isready -U iosforge -d iosforge   # -> accepting connections
curl -fsS http://localhost:9000/minio/health/live      # -> HTTP 200

# Inspect buckets (mc on the compose network)
docker run --rm --network iosforge_default --entrypoint sh minio/mc:latest -c \
  'mc alias set local http://iosforge-minio:9000 iosforge-dev iosforge-dev-secret && mc ls local'
```

## Notes
- The artifacts bucket (`iosforge-artifacts`) has **object versioning enabled** — required
  for artifact version history (SPEC §8, §10; used later by T-2.3 storage abstraction).
- Restart policy is `unless-stopped` for the long-running services; `minio-init` is `no`.
