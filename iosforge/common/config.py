"""Application settings (pydantic-settings).

Reads configuration from environment variables and, optionally, from a config
file under ``configs/`` (``configs/app.toml`` by default, or a ``.env``-style
file). Secrets (S3 keys, match/ASC credentials) come from the environment or a
secret store only — they are never hardcoded here.

Thresholds and walkthrough limits are exposed as plain fields with the defaults
agreed in ``state/DECISIONS.md`` (Q1, Q3) so they can later be overridden via
config/env without a redeploy (SPEC §8, §9).
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# Repository root: iosforge/common/config.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIGS_DIR = _REPO_ROOT / "configs"
_DEFAULT_CONFIG_FILE = _CONFIGS_DIR / "app.toml"


def _load_toml_config(path: Path) -> dict[str, Any]:
    """Load a flat ``key = value`` TOML config file, returning {} if absent."""
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    # Only top-level scalar/sequence values are used as settings overrides;
    # nested tables are ignored to keep the precedence model simple.
    return {key: value for key, value in data.items() if not isinstance(value, dict)}


class TomlConfigSettingsSource(PydanticBaseSettingsSource):
    """Settings source that reads overrides from a ``configs/*.toml`` file.

    Precedence (highest first): explicit init kwargs > environment > .env file
    > TOML config file > field defaults. This lets per-app config files live in
    ``configs/`` while env/secret still win for sensitive values.
    """

    def __init__(self, settings_cls: type[BaseSettings], path: Path) -> None:
        super().__init__(settings_cls)
        self._data = _load_toml_config(path)

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:  # noqa: ANN401
        value = self._data.get(field_name)
        return value, field_name, False

    def __call__(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field_name in self.settings_cls.model_fields:
            value, key, _ = self.get_field_value(None, field_name)
            if value is not None:
                result[key] = value
        return result


class Settings(BaseSettings):
    """Typed application settings.

    Field names map to ``UPPER_SNAKE`` env vars (case-insensitive). See
    ``.env.example`` for the canonical variable names.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App-level ---
    env: str = "local"
    debug: bool = False

    # --- Database (PostgreSQL) ---
    database_url: str = "postgresql+psycopg://iosforge:iosforge@localhost:5432/iosforge"

    # --- Celery broker / result backend (Redis) ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Artifact storage (MinIO / S3-compatible) ---
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket: str = "iosforge-artifacts"
    s3_region: str = "us-east-1"

    # --- Compliance metric (DECISIONS Q1) — overridable via config, no redeploy ---
    compliance_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    compliance_soft_floor: float = Field(default=0.80, ge=0.0, le=1.0)
    compliance_weight_visual: float = Field(default=0.5, ge=0.0, le=1.0)
    compliance_weight_coverage: float = Field(default=0.3, ge=0.0, le=1.0)
    compliance_weight_flows: float = Field(default=0.2, ge=0.0, le=1.0)
    compliance_max_iterations: int = Field(default=3, gt=0)

    # --- Walkthrough limits (DECISIONS Q3) — placeholders, per-job overridable ---
    walkthrough_max_screens: int = Field(default=40, gt=0)
    walkthrough_max_depth: int = Field(default=6, gt=0)
    walkthrough_job_timeout_s: int = Field(default=1200, gt=0)
    walkthrough_action_timeout_s: int = Field(default=15, gt=0)
    # Walkthrough-only mode: finish a Job (DONE) right after the emulator crawl,
    # skipping the codegen/compliance stage (e.g. when Flutter is unavailable).
    pipeline_stop_after_walkthrough: bool = Field(default=False)
    # Analysis-only mode: run screen-filter + analyze, then finish (DONE) —
    # skips decompose/codegen/compliance. For inspecting the App Spec cheaply.
    pipeline_stop_after_analyze: bool = Field(default=False)
    # Admin/backend deliverable (Firebase + Rowy + RevenueCat + Stream): emit an
    # admin/ scaffold alongside the app when app_spec.backend.admin_panel_needed.
    # provision_admin (opt-in) would create the live Firebase project — needs creds.
    generate_admin: bool = Field(default=True)
    provision_admin: bool = Field(default=False)
    admin_backend_provider: str = Field(default="firebase_rowy")
    # Firebase provisioning credentials (paths/keys only — real secret file lives
    # outside the repo, referenced from .env). Empty = provisioning is skipped.
    firebase_sa_path: str = Field(default="")
    firebase_project_id: str = Field(default="")
    firebase_create_project: bool = Field(default=False)
    revenuecat_api_key: str = Field(default="")
    # --- Video-frame ingestion: screens come from an uploaded screen-recording
    # instead of an emulator crawl. Frames are sampled at video_frame_fps to keep
    # transitions (modals, dropdowns, appearance animations) visible; mpdecimate
    # (video_dedup) collapses only truly static holds. video_max_frames caps output.
    video_frame_fps: int = Field(default=4, gt=0)
    video_dedup: bool = Field(default=True)
    video_max_frames: int = Field(default=80, gt=0)
    # Before analysis, classify each frame (vision) and drop non-app frames —
    # full-screen ads and the iOS home/lock screen captured in the recording.
    filter_junk_frames: bool = Field(default=True)
    # On-demand ad analysis: read the marked ad frames + monetization/rewards
    # screens and emit a structured ad model (networks best-effort + placements).
    # Off by default to save vision tokens; ad frames are always kept/marked.
    analyze_ads: bool = Field(default=False)
    # Stage 3 codegen orchestration: "claude" = single local Claude CLI task runner;
    # "hermes" = Hermes Agent orchestrates, local Claude CLI executes (Variant A);
    # "cloud" = the local Claude Code agent orchestrates AND builds directly.
    codegen_orchestrator: str = Field(default="claude")
    # Max concurrent screen-build workers when the Hermes orchestrator parallelizes
    # the screen layer (worktree-per-task). Caps rate-limit / subscription pressure.
    codegen_max_parallel: int = Field(default=4, gt=0)

    # --- Admin panel (SPEC §7) — internet-facing behind a TLS reverse proxy ---
    # Secret for signing session ids / CSRF tokens. MUST be set via env in prod.
    admin_session_secret: str = ""
    admin_session_ttl_s: int = Field(default=3600, gt=0)
    # Cookie Secure flag — keep True in prod (HTTPS). Set False only for local http dev.
    admin_cookie_secure: bool = True
    admin_upload_max_bytes: int = Field(default=200 * 1024 * 1024, gt=0)  # 200 MiB
    admin_login_max_attempts: int = Field(default=5, gt=0)
    admin_login_lockout_s: int = Field(default=300, gt=0)
    admin_avd: str = "mvp"  # AVD the worker boots for the walkthrough
    # Seed-only operator credentials (used by `python -m iosforge.admin.seed`).
    # Never referenced at request time; password is hashed into admin_users.
    admin_seed_username: str = ""
    admin_seed_password: str = ""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_settings = TomlConfigSettingsSource(settings_cls, _DEFAULT_CONFIG_FILE)
        # init > env > .env > toml file > secrets/defaults
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            toml_settings,
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings instance."""
    return Settings()
