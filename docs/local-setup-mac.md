# Local setup on a Mac — clone → running admin

This runbook takes a clean Mac to a working local IOSForge factory: Postgres, Redis and MinIO in
Docker, the four Celery workers and the admin at http://127.0.0.1:8070. Every command here
is one we actually ran to bring the stack up. Run the commands from the repo root unless a
step says otherwise.

Reference machine: macOS 26.4 (Apple Silicon), Docker Desktop 29.6, Xcode 26.6, XcodeGen
2.46, uv 0.11, Node 22 (for the claude CLI), Claude Code 2.1, ffmpeg 9.0.

## 1. Prerequisites

```bash
# Homebrew (skip if `brew --version` works)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# uv (Python toolchain; installs the Python >= 3.12 the project pins on `uv sync`)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Docker runtime: Docker Desktop (what we use). Start it once so the daemon is running.
brew install --cask docker
open -a Docker
docker info >/dev/null && echo "docker daemon up"

# Xcode from the App Store (full Xcode, not just the CLT), then:
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
sudo xcodebuild -license accept
xcodebuild -runFirstLaunch
xcodebuild -downloadPlatform iOS          # iOS Simulator runtime (needed by the build/judge)

# Build + media tools the workers shell out to
brew install xcodegen ffmpeg

# claude CLI: the analysis, scope and codegen stages run it as a subprocess
brew install node                          # or nvm; any Node >= 18
npm install -g @anthropic-ai/claude-code
claude                                     # interactive once: log in, then /exit
```

The workers inherit `PATH` from the shell that runs `scripts/factory.sh`. Check that the
binaries resolve:

```bash
command -v uv docker claude xcodegen xcrun ffmpeg
```

Optional: `chromium` / Google Chrome is used only by the HTML store-assets engine. The
Android SDK path (`scripts/install_android_toolchain.sh`) is the legacy APK input and is
not needed for the Frida-archive flow.

## 2. Clone and configure

```bash
git clone git@github.com:ScheglovIvan/IOSForge.git
cd IOSForge
git checkout feature/swiftui-pivot

cp .env.example .env
```

Edit `.env`. The defaults already match the ports below. Set at least these:

| Variable | Local value | Why |
|---|---|---|
| `ADMIN_SEED_USERNAME` | e.g. `admin` | operator account created in step 4 |
| `ADMIN_SEED_PASSWORD` | a password you choose | same |
| `ADMIN_COOKIE_SECURE` | `false` | the local admin is plain http; with `true` login loops back to `/login` |
| `PIPELINE_SCOPE_GATE` | `true` (default in the template) | the job stops at the Scope tab for operator approval |
| `IOS_SIMULATOR_UDID` | UDID of an iPhone simulator (step 6) | the Vision Judge renders on it; empty = first available iPhone |
| `CODEGEN_GENERATE_IMAGES` | `false` | `true` spends money on Replicate |
| `IOS_DELIVERY_UPLOAD` | `false` | `true` uploads to App Store Connect |

Leave the port/URL block as in the template. The values must stay in sync with each other:

```
DATABASE_URL=postgresql+psycopg://iosforge:iosforge@localhost:5440/iosforge
POSTGRES_PORT=5440
REDIS_URL=redis://localhost:6379/0
S3_ENDPOINT_URL=http://localhost:9010
MINIO_API_PORT=9010
MINIO_CONSOLE_PORT=9001
```

### Secrets (optional, all gitignored under `secrets/`)

Nothing in `secrets/` is required to bring the admin up or to run a job to an unsigned IPA.
Each file is read only when its feature is used:

```bash
mkdir -p secrets && chmod 700 secrets

# Replicate: paid image generation for decorative slots and store slides
printf 'REPLICATE_API_TOKEN=%s\n' '<your token from replicate.com/account/api-tokens>' > secrets/replicate.env
chmod 600 secrets/replicate.env
```

Other optional files are `secrets/apphud.env` (shared Apphud SDK key) and
`secrets/tenjin.env` (Tenjin key). Per-app App Store Connect keys are uploaded in the admin
(job → Build & sign) and land in `secrets/jobs/<job_id>/`. Never commit any of these files.

## 3. Infrastructure: Postgres, Redis, MinIO

```bash
docker compose up -d
docker compose ps -a     # postgres/redis/minio "healthy"; minio-init exited (0)
docker logs iosforge-minio-init   # Bucket created successfully `local/iosforge-artifacts`.
```

The MinIO images come from `cgr.dev/chainguard/` because Docker Hub no longer serves
`minio/minio` or `minio/mc`. The first `up` pulls them, and no registry login is needed.

Host ports: Postgres **5440**, Redis **6379**, MinIO S3 API **9010**, MinIO console **9001**.
The containers use `restart: unless-stopped`, so they come back on their own whenever Docker
Desktop starts.

## 4. Python env, database schema, operator account

```bash
uv sync
uv run alembic upgrade head
uv run python -m iosforge.admin.seed      # reads ADMIN_SEED_USERNAME / ADMIN_SEED_PASSWORD from .env
```

## 5. Start the factory

```bash
scripts/factory.sh start     # intake, codegen, delivery, xcode workers + admin
scripts/factory.sh status    # all five "up"
```

| Process | Queues |
|---|---|
| intake | `discovery,acquisition,walkthrough` |
| codegen | `codegen` (analysis, scope proposal) |
| delivery | `delivery` |
| xcode | `xcode` (SwiftUI build, Vision Judge, rework, IPA export) |
| admin | uvicorn on `127.0.0.1:8070` |

Every worker runs `--concurrency=1`. Logs and pid files go to `var/factory/` (gitignored).
Use `scripts/factory.sh logs xcode` to tail a log, and `stop` / `restart` to control the
processes. `FACTORY_LOGS`, `ADMIN_HOST` and `ADMIN_PORT` override the defaults.

## 6. Smoke check

```bash
curl -s http://127.0.0.1:8070/health                 # {"status":"ok"}
grep -c 'ready.' var/factory/{intake,codegen,delivery,xcode}.log   # each >= 1
```

Open http://127.0.0.1:8070 and sign in with `ADMIN_SEED_USERNAME` / `ADMIN_SEED_PASSWORD`.
The Jobs list should load.

## 7. iOS build (what a job needs to reach an IPA)

The xcode worker generates a SwiftUI project, runs `xcodegen`, then `xcodebuild` builds and
renders the app on an iOS Simulator for the Vision Judge, and archives an IPA.

- **Simulator:** our builds use an **iPhone 15 Pro** on the **iOS 17.2** runtime. Any
  installed iPhone simulator works. Pick one and put its UDID in `.env`:

  ```bash
  xcrun simctl list devices available | grep iPhone
  # optional: create one if the list is empty
  xcrun simctl create "iPhone 15 Pro" "iPhone 15 Pro" "$(xcrun simctl list runtimes | awk '/iOS/{print $NF}' | tail -1)"
  ```

  Then set `IOS_SIMULATOR_UDID=<udid>` and run `scripts/factory.sh restart`.
- **XcodeGen / Xcode:** `xcodegen --version` and `xcodebuild -version` must both work in the
  shell that starts the factory.
- **Signing:** without a key the job delivers an **unsigned IPA**, which the admin offers
  under job → iOS build → Download IPA. Signed and App Store builds need a per-app ASC key
  (Build & sign tab) and the `store` profile. Those builds are out of scope for local runs.

## 8. Run a job from the admin

1. **New job**: enter the App Store URL and a Frida capture archive. Upload the collector's
   own `archive.zip`. Do not re-zip a capture folder: extra files fail the integrity check.
2. The Overview tab shows the stage timeline. It refreshes on its own.
3. **Scope**: review screens, capabilities and tiers, adjust the checkboxes, then click
   **Approve & generate**.
4. When the build finishes, use **View artifacts** (screenshots, screen map, compliance
   score) and **iOS build → Download IPA**.
5. **Rework**: use **Apply rework** (free-text changes) or **Add screens** (scope extension)
   on a finished or held job.

`uv run python scripts/job_status.py <job_id>` prints a job's state, timeline and builds
from the database.

## Troubleshooting

- Login keeps returning to `/login`: set `ADMIN_COOKIE_SECURE=false` and run
  `scripts/factory.sh restart`.
- Worker log shows `Cannot connect to redis://localhost:6379`: Docker Desktop is not running
  or the containers are down. Run `open -a Docker`, then `docker compose up -d`.
- Ports 5440, 9010 or 8070 are already in use: change `POSTGRES_PORT` together with
  `DATABASE_URL`, `MINIO_API_PORT` together with `S3_ENDPOINT_URL`, or set `ADMIN_PORT`.
