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
    # Anti-clone goal (structure-preserving redesign): the score rewards STRUCTURAL
    # fidelity (same blocks/placement/navigation) and DIVERGENCE (looks unlike the
    # source), not visual similarity. `divergence_min` is a hard floor — below it a
    # screen is a clone risk and keeps iterating even if the weighted score passes.
    compliance_weight_structure: float = Field(default=0.4, ge=0.0, le=1.0)
    compliance_weight_coverage: float = Field(default=0.3, ge=0.0, le=1.0)
    compliance_weight_flows: float = Field(default=0.1, ge=0.0, le=1.0)
    compliance_weight_divergence: float = Field(default=0.2, ge=0.0, le=1.0)
    compliance_divergence_min: float = Field(default=0.4, ge=0.0, le=1.0)
    compliance_max_iterations: int = Field(default=3, gt=0)
    # Web screen-similarity verification: after frontend codegen, build the app
    # for web and render each screen in headless Chromium via the canonical preview
    # route /#/screen/<id>, then vision-judge vs the originals. Non-fatal — a build
    # or render failure never loses the generated frontend. chromium_bin empty =
    # auto-discover (chromium / chromium-browser / google-chrome).
    verify_frontend_web: bool = Field(default=True)
    # Compile gate: after codegen (and augment/rework) run `flutter analyze`; if it reports
    # ERROR-severity issues, run a bounded rework fix loop so non-compiling code never reaches
    # GitHub / the CodeMagic iOS build (which would otherwise waste a full build to surface it).
    codegen_compile_gate: bool = Field(default=True)
    codegen_compile_gate_attempts: int = Field(default=2, ge=0, le=5)
    # Frontend web-verify pass bar. Distinct from the SPEC ≥0.95 iOS-compliance goal
    # (compliance_threshold): a headless web render of a Flutter app compared to
    # native iOS screenshots has an inherent ceiling, so the frontend MVP passes at
    # 0.80 similarity.
    frontend_verify_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    web_render_wait_ms: int = Field(default=15000, gt=0)
    web_render_window: str = Field(default="390,844")
    chromium_bin: str = Field(default="")
    # Structural web verification (Stage VERIFY): static audit of the generated
    # flutter_app/lib against the screens.json nav graph — nav_audit (missing
    # screens / dead links / missing edges) + blank_screens heuristic. No runtime
    # browser, no new deps.
    verify_web_structural: bool = Field(default=True)
    # Auto end-of-pipeline hard-gated verify loop (build_web → render → evaluate →
    # nav_audit → fix-tasks → codegen → re-audit) instead of a single-pass
    # verify_web. Opt-in — the codegen host needs Flutter web + Chromium.
    pipeline_web_verify_loop: bool = Field(default=False)
    # On loop exhaustion with structural gaps still open, end the Job in
    # NEEDS_INPUT (human decides ship/rework) instead of silently DONE.
    web_verify_hard_gate: bool = Field(default=True)
    # Blank-render byte threshold: a rendered screen PNG smaller than this for a
    # 390x844 window is treated as a near-uniform / blank screen (no image lib).
    web_blank_max_bytes: int = Field(default=6000, gt=0)

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
    # Admin/backend deliverable (Firebase + Rowy + Apphud + Stream): emit an
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
    # Per-app model: each clone gets its own GCP/Firebase project. Parent org/folder
    # resource for project creation ("organizations/…"/"folders/…"); empty means no
    # parent — a service account almost always needs an org + projectCreator + quota,
    # so parentless create typically fails (logged as a warning).
    firebase_parent: str = Field(default="")
    firebase_project_prefix: str = Field(default="iosforge")
    # Generate the backend-connected Flutter app (flutter_wiring) wired to the
    # provisioned project's web config, alongside the static codegen output.
    generate_wired_app: bool = Field(default=True)
    # Apphud subscriptions. Apphud has no provisioning REST API (apps/products/
    # paywalls/placements are configured once in the Apphud dashboard), so the
    # pipeline only injects the public SDK key (app_…) + the spec's product ids into
    # the generated app; the SDK auto-detects sandbox vs production via StoreKit.
    # The key lives in env (never in code). Empty key = payment wiring is skipped.
    apphud_api_key: str = Field(default="")
    apphud_provision: bool = Field(default=True)
    # SDK key (appstr_…/app_…) may instead live in a gitignored env file
    # (APPHUD_API_KEY=…); per-job override goes in Job.source_app_metadata so each
    # clone gets its own app's key. Path is relative to the worker CWD.
    apphud_secrets_path: str = Field(default="secrets/apphud.env")
    # Placement identifier configured in Apphud (Product Hub > Placements) that the
    # paywall loads via Apphud.placements(); the derived per-clone default is used
    # when empty so codegen always has a lookup key.
    apphud_placement: str = Field(default="")
    # --- Attribution (MMP): Tenjin. Traffic sources (Meta/Google/TikTok/ASA and any
    # other network) are connected in the Tenjin dashboard — nothing about them is
    # baked into the app, so a new source needs no rebuild. Only the iOS SDK key is
    # injected; it lives in a gitignored env file (TENJIN_API_KEY=…). Per-job override
    # goes in Job.source_app_metadata. Empty key = attribution wiring is skipped.
    attribution_provision: bool = Field(default=True)
    tenjin_api_key: str = Field(default="")
    tenjin_secrets_path: str = Field(default="secrets/tenjin.env")
    # Consolidated SKAdNetworkItems plist downloaded from the MMP dashboard (the
    # authoritative, maintained list — public registries go stale). Merged into
    # Info.plist at build time so a network added later still attributes without an
    # app update. Missing file = the merge step is a no-op.
    skadnetwork_ids_path: str = Field(default="configs/skadnetwork_ids.plist")
    # App Store listing screenshots: the planner studies the source listing for
    # composition only and picks which of OUR rendered screens backs each claim.
    # Stock backdrops come from Pexels (commercial use, no attribution required).
    store_assets_timeout_s: int = Field(default=900, gt=0)
    pexels_secrets_path: str = Field(default="secrets/pexels.env")
    # ATT prompt copy (Info.plist NSUserTrackingUsageDescription). Without ATT consent
    # attribution degrades to SKAN campaign-level aggregates (no keyword/creative).
    att_usage_description: str = Field(
        default="Allow tracking so we can measure which ads bring people here "
        "and keep improving the app for you."
    )
    # --- Media hosting: Cloudflare R2 (S3-compatible) — no Firebase Storage/Blaze.
    # Endpoint + access key + secret live in a gitignored env file referenced by
    # r2_secrets_path (e.g. secrets/r2.env with R2_ENDPOINT / R2_ACCESS_KEY_ID /
    # R2_SECRET_ACCESS_KEY); bucket + public base URL are non-secret. One shared
    # bucket, per-app key prefix (= collection_prefix). Empty bucket = R2 disabled.
    r2_secrets_path: str = Field(default="")
    r2_bucket: str = Field(default="")
    r2_public_base: str = Field(default="")
    # --- Fully automatic pipeline: Analysis auto-chains into Frontend Build +
    # Code Generation, then GitHub Upload (no manual button). ---
    auto_build_frontend: bool = Field(default=True)
    # Per-task codegen retry budget (Code Generation "Retrying…" then permanent-fail).
    codegen_task_max_attempts: int = Field(default=2, gt=0)
    # GitHub Upload stage: push the generated project to a new public repo. Token
    # (scope repo/public_repo) lives in a gitignored env file (github_token_path,
    # GITHUB_TOKEN=...). Empty token / publish off = the stage is skipped.
    github_publish: bool = Field(default=True)
    github_token_path: str = Field(default="")
    github_api_base: str = Field(default="https://api.github.com")
    github_repo_private: bool = Field(default=False)
    github_repo_prefix: str = Field(default="")
    # CodeMagic Integration stage: import the pushed repo as a CodeMagic app and
    # commit a codemagic.yaml (iOS). No build/sign/IPA/TestFlight here. Token
    # (x-auth-token) lives in a gitignored env file (codemagic_token_path,
    # CODEMAGIC_TOKEN=...). Empty token / off = the stage is skipped.
    codemagic_integration: bool = Field(default=True)
    codemagic_token_path: str = Field(default="")
    codemagic_api_base: str = Field(default="https://api.codemagic.io")
    codemagic_team_id: str = Field(default="")
    codemagic_sync_timeout_s: int = Field(default=120, gt=0)
    # Reference codemagic.yaml: when set to a proven project ("owner/repo") its config
    # is fetched and reused (substituting only the per-app bundle id). Empty (default) →
    # the built-in UNSIGNED iOS template (sideload / jailbreak, no Apple signing).
    codemagic_template_repo: str = Field(default="")
    codemagic_bundle_prefix: str = Field(default="com.batteam")
    # Auto-trigger the CodeMagic iOS build at the end of the pipeline (instead of the
    # manual button). Every run spends CodeMagic minutes; the template is UNSIGNED.
    codemagic_auto_build: bool = Field(default=True)
    # App Store Connect API key for a future SIGNED "real" build (Apple Dev account
    # required). Empty for now — the "real" profile still produces an unsigned but
    # correctly-branded build (real bundle id / name) until these are provided.
    appstore_connect_key_path: str = Field(default="")
    appstore_connect_key_id: str = Field(default="")
    appstore_connect_issuer_id: str = Field(default="")
    # Seed placeholder CONTENT (series/episodes + tiny dummy videos in Storage) so
    # the generated app is visually reviewable; swap for real content later.
    seed_placeholder_content: bool = Field(default=True)
    # Before analysis, classify each frame (vision) and drop non-app frames —
    # full-screen ads and the iOS home/lock screen captured in the recording.
    filter_junk_frames: bool = Field(default=True)
    # On-demand ad analysis: read the marked ad frames + monetization/rewards
    # screens and emit a structured ad model (networks best-effort + placements).
    # Off by default to save vision tokens; ad frames are always kept/marked.
    analyze_ads: bool = Field(default=False)
    # Build clones WITHOUT any advertising (owner policy): analyze strips
    # ad_networks/ad_placements/ads from app_spec and instructs no ad slots;
    # codegen adds no ad SDKs/widgets. Set False to reproduce the app's ads.
    no_ads: bool = Field(default=True)
    # Stage 3 codegen orchestration: "claude" = single local Claude CLI task runner;
    # "hermes" = Hermes Agent orchestrates, local Claude CLI executes (Variant A);
    # "cloud" = the local Claude Code agent orchestrates AND builds directly.
    codegen_orchestrator: str = Field(default="claude")
    # Max concurrent screen-build workers when the orchestrator parallelizes the
    # screen layer (worktree-per-task). Caps rate-limit / subscription pressure.
    codegen_max_parallel: int = Field(default=4, gt=0)
    # Design divergence (anti-clone): after analysis, rewrite app_spec design_tokens
    # into a NEW visual language (hue-rotated palette + gradients + shifted radii)
    # while preserving structure/navigation. Set False to reproduce a faithful clone.
    design_divergence: bool = Field(default=True)
    # Swap captured fonts for similar-but-different families (same typographic class).
    design_font_substitution: bool = Field(default=True)
    # Content-level anti-clone: codegen paraphrases user-facing copy (same meaning,
    # different wording), diverges icon style and drops the source app's brand marks
    # (logo/wordmark). Photographic content assets are kept. False = verbatim copy.
    design_content_divergence: bool = Field(default=True)

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
    # --- App Store ingestion: the source app is identified by its official App
    # Store URL; the worker resolves name/description/screenshots via the iTunes
    # Lookup API during the WALKTHROUGH stage. Screenshots are kept as reference
    # metadata only (not the pipeline screens yet).
    appstore_api_base: str = "https://itunes.apple.com"
    appstore_country_default: str = "us"
    appstore_fetch_timeout_s: int = Field(default=20, gt=0)
    appstore_max_screenshots: int = Field(default=20, gt=0)
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
