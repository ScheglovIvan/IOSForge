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

## Mobile toolchain (Android SDK + emulator + Flutter)

The emulator/crawl stage (`iosforge/mvp/emulator.py`) drives a host-installed Android SDK
via `adb`/`emulator` and expects an AVD named `mvp` (`ANDROID_SDK_ROOT`, default
`/opt/android-sdk`; `ADMIN_AVD=mvp`). Install everything with the idempotent script:

```bash
sudo bash scripts/install_android_toolchain.sh      # JDK17 + SDK + AVD 'mvp' + Flutter
source /etc/profile.d/android-sdk.sh                 # load ANDROID_SDK_ROOT + PATH
```

Installs: JDK 17, Android command-line tools → `/opt/android-sdk`, `platform-tools`,
`emulator`, `platforms;android-34`, `build-tools;34.0.0`, system image
`android-34;google_apis;x86_64`, AVD `mvp` (pixel_6), and Flutter stable in `/opt/flutter`.
Override defaults via env vars (`API_LEVEL`, `SYSTEM_IMAGE`, `AVD_NAME`, `CMDLINE_TOOLS_VER`).

Requirements: Ubuntu x86_64, `/dev/kvm` present (hardware accel). The emulator runs headless
(`-no-window -gpu swiftshader_indirect`). A non-root runtime user must be in the `kvm` group:

```bash
sudo usermod -aG kvm "$USER"      # then re-login
```

### Smoke test

```bash
emulator -list-avds                                  # -> mvp
emulator -avd mvp -no-window -no-audio -no-boot-anim -gpu swiftshader_indirect &
adb wait-for-device && adb shell getprop sys.boot_completed    # -> 1
flutter doctor                                       # Android toolchain OK
```
