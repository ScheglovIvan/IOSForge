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
    # Wall-clock budget for a single rework/augment `claude` codegen pass. The default
    # 1800s is too tight for a substantial rework (rebrand + several new features across a
    # large app), where the CLI hits the limit and PipelineTask retries burn hours. Tunable
    # via env so a big repositioning pass can run without a code change.
    codegen_rework_timeout_s: int = Field(default=5400, gt=0)
    # Vision-Judge pass bar of the generated SwiftUI app on the iOS Simulator. Distinct
    # from the SPEC ≥0.95 iOS-compliance goal (compliance_threshold): the clone must
    # diverge visually by design, so the MVP passes at 0.80.
    frontend_verify_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    # Headless Chromium for the store-slide renderer (empty = discover on PATH).
    chromium_bin: str = Field(default="")
    # With structural gaps still open after refine (or no audit), end the Job in
    # NEEDS_INPUT (human decides ship/rework) instead of DONE. A clone-risk app is
    # held regardless of this flag; visual scores alone never hold.
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
    # skipping analysis and the SwiftUI build (legacy APK path).
    pipeline_stop_after_walkthrough: bool = Field(default=False)
    # Analysis-only mode: run screen-filter + analyze, then finish (DONE) —
    # skips the SwiftUI build. For inspecting the App Spec cheaply.
    pipeline_stop_after_analyze: bool = Field(default=False)
    # Scope gate (Stage SCOPE): after analysis, propose an MVP scope + iOS
    # feasibility report, persist scope.json(proposed) and park the Job in
    # NEEDS_INPUT for an operator to approve/edit before codegen (then build_swiftui
    # prunes app_spec to the approved screens). Opt-in — default False keeps the
    # auto-chain (ANALYSIS -> build_swiftui over every screen).
    pipeline_scope_gate: bool = Field(default=False)
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
    # Each rendered slide is scored against the source slide's composition (palette,
    # wording and screen content are meant to differ and are not penalised). Slides
    # below the threshold are re-authored from the review's defect list.
    store_assets_similarity_min: int = Field(default=80, ge=0, le=100)
    store_assets_max_iterations: int = Field(default=3, gt=0)
    # Which engine draws a slide. "replicate" sends the source slide to an image
    # model and gets a finished slide back in about a minute; "html" has an agent
    # author a self-contained page that headless Chromium screenshots, which costs
    # minutes per slide but keeps every glyph and colour under our control.
    store_assets_engine: str = Field(default="replicate", pattern="^(replicate|html)$")
    replicate_secrets_path: str = Field(default="secrets/replicate.env")
    # Seedream renders explicit dimensions, so the App Store canvas comes out of
    # the model directly — no aspect-ratio approximation, no post-processing.
    replicate_model: str = Field(default="bytedance/seedream-4")
    # The model only emits stock aspect ratios, so 9:16 at 4K is generated and the
    # exact App Store canvas is reached afterwards by growing the background.
    replicate_resolution: str = Field(default="4K")
    replicate_timeout_s: int = Field(default=600, gt=0)
    # SwiftUI codegen: generate divergent replacement images for decorative photos the
    # capture could not provide (Replicate, costs money). Off = local placeholders.
    codegen_generate_images: bool = Field(default=False)
    # Native delivery (SwiftUI): Apple Developer team for automatic signing; empty means
    # an unsigned archive + unsigned IPA only. Uploading to App Store Connect is an
    # external action and stays off unless explicitly enabled.
    xcode_team_id: str = Field(default="")
    xcode_archive_timeout_s: int = Field(default=3600, gt=0)
    ios_delivery_upload: bool = Field(default=False)
    # iOS Simulator the Mac worker renders on (Vision Judge); empty = not configured.
    ios_simulator_udid: str = Field(default="")
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
    # --- Fully automatic pipeline: Analysis (or the approved scope) auto-chains into
    # the SwiftUI build on the Mac worker, then native delivery (no manual button). ---
    auto_build_frontend: bool = Field(default=True)
    github_token_path: str = Field(default="")
    github_api_base: str = Field(default="https://api.github.com")
    github_repo_private: bool = Field(default=False)
    # Legal pages stage: the Privacy Policy published to a pages repo's gh-pages
    # branch needs a reachable contact address, which App Store review checks.
    legal_contact_email: str = Field(default="support@iosforge.app")
    codemagic_bundle_prefix: str = Field(default="com.batteam")
    # App Store Connect signing credential uploaded PER JOB in the admin (each app ships
    # under its own Apple account, so the .p8 / Issuer ID / Key ID are per app). Stored in
    # gitignored files under secrets/jobs/<job_id>/, never committed, never in .env, chmod
    # 600. Native delivery passes the key to xcodebuild (-allowProvisioningUpdates) and to
    # altool by path; a reusable RSA certificate key is generated once per app. A credential
    # dropped in the shared paths
    # below (no UI) is an optional fallback when a job has none of its own.
    asc_jobs_secrets_dir: str = Field(default="secrets/jobs")
    asc_api_key_secrets_path: str = Field(default="secrets/asc_api_key.env")
    asc_api_key_p8_path: str = Field(default="secrets/asc_api_key.p8")
    asc_certificate_key_path: str = Field(default="secrets/asc_certificate_private_key.pem")
    # Upper bound on an uploaded App Store Connect .p8 (they are ~250 bytes).
    asc_api_key_max_bytes: int = Field(default=16 * 1024, gt=0)
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
    # Max concurrent screen-build workers when the orchestrator parallelizes the
    # screen layer (worktree-per-task). Caps rate-limit / subscription pressure.
    codegen_max_parallel: int = Field(default=4, gt=0)
    # Design divergence (anti-clone): after analysis, rewrite app_spec design_tokens
    # into a NEW visual language (hue-rotated palette + gradients + shifted radii)
    # while preserving structure/navigation. Set False to reproduce a faithful clone.
    design_divergence: bool = Field(default=True)
    # Swap captured fonts for similar-but-different families (same typographic class).
    design_font_substitution: bool = Field(default=True)

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
