"""Tests for the config loader (T-1.3, SPEC §8/§9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from iosforge.common import config
from iosforge.common.config import Settings, get_settings

_IOSFORGE_ENV_KEYS = (
    "DATABASE_URL",
    "REDIS_URL",
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_BUCKET",
    "ENV",
    "DEBUG",
    "COMPLIANCE_THRESHOLD",
    "COMPLIANCE_SOFT_FLOOR",
    "WALKTHROUGH_MAX_SCREENS",
    "WALKTHROUGH_MAX_DEPTH",
    "WALKTHROUGH_JOB_TIMEOUT_S",
)


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hermetic: drop ambient IOSForge env so default-asserting tests are
    # reproducible regardless of the shell that launched pytest. Tests that
    # need a value set it explicitly via monkeypatch.setenv after this runs.
    for key in _IOSFORGE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()


def test_defaults_match_decisions() -> None:
    """Placeholder defaults come from DECISIONS Q1/Q3."""
    settings = Settings()
    assert settings.env == "local"
    assert settings.debug is False
    assert settings.compliance_threshold == 0.95
    assert settings.compliance_soft_floor == 0.80
    assert settings.compliance_weight_visual == 0.5
    assert settings.compliance_weight_coverage == 0.3
    assert settings.compliance_weight_flows == 0.2
    assert settings.walkthrough_max_screens == 40
    assert settings.walkthrough_max_depth == 6
    assert settings.walkthrough_job_timeout_s == 1200
    assert settings.walkthrough_action_timeout_s == 15
    assert settings.s3_bucket == "iosforge-artifacts"


def test_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables override field defaults."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/x")
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/1")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "AKIAFROMENV")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "secretfromenv")
    monkeypatch.setenv("COMPLIANCE_THRESHOLD", "0.9")
    monkeypatch.setenv("WALKTHROUGH_MAX_SCREENS", "12")

    settings = Settings()

    assert settings.database_url == "postgresql+psycopg://u:p@db:5432/x"
    assert settings.redis_url == "redis://cache:6379/1"
    assert settings.s3_access_key_id == "AKIAFROMENV"
    assert settings.s3_secret_access_key == "secretfromenv"
    assert settings.compliance_threshold == 0.9
    assert settings.walkthrough_max_screens == 12


def test_init_kwargs_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit constructor kwargs win over the environment."""
    monkeypatch.setenv("ENV", "production")
    settings = Settings(env="staging")
    assert settings.env == "staging"


def test_reads_from_toml_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A configs/*.toml file overrides defaults but env still wins over it."""
    cfg_file = tmp_path / "app.toml"
    cfg_file.write_text(
        "\n".join(
            [
                'env = "ci"',
                "compliance_threshold = 0.85",
                "walkthrough_max_screens = 7",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_DEFAULT_CONFIG_FILE", cfg_file)
    # Env wins over the TOML file for this one field.
    monkeypatch.setenv("WALKTHROUGH_MAX_SCREENS", "99")

    settings = Settings()

    assert settings.env == "ci"
    assert settings.compliance_threshold == 0.85
    assert settings.walkthrough_max_screens == 99  # env beats toml


def test_get_settings_is_cached() -> None:
    """get_settings returns the same cached instance."""
    assert get_settings() is get_settings()


def test_secrets_not_hardcoded() -> None:
    """Secret fields default to empty — they must come from env/secret store."""
    settings = Settings()
    assert settings.s3_access_key_id == ""
    assert settings.s3_secret_access_key == ""
