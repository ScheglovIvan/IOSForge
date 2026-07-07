"""R2 media-host helper: credential loading + public URL mapping (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from iosforge.common.config import Settings
from iosforge.mvp import r2


def _settings(tmp_path: Path, body: str) -> Settings:
    secrets = tmp_path / "r2.env"
    secrets.write_text(body)
    return Settings(
        r2_secrets_path=str(secrets),
        r2_bucket="iosforge-media",
        r2_public_base="https://pub-abc.r2.dev/",
    )


def test_load_credentials_from_file(tmp_path: Path) -> None:
    s = _settings(
        tmp_path,
        "# Cloudflare R2\n"
        'R2_ENDPOINT="https://acc.r2.cloudflarestorage.com"\n'
        "R2_ACCESS_KEY_ID=key123\n"
        "R2_SECRET_ACCESS_KEY=secret456\n"
        "OTHER=ignored\n",
    )
    creds = r2.load_credentials(s)
    assert creds == {
        "endpoint": "https://acc.r2.cloudflarestorage.com",
        "access_key_id": "key123",
        "secret": "secret456",
    }


def test_load_credentials_missing_raises(tmp_path: Path) -> None:
    s = _settings(tmp_path, "R2_ENDPOINT=https://x\n")  # no key/secret
    with pytest.raises(r2.R2ConfigError, match="R2_ACCESS_KEY_ID"):
        r2.load_credentials(s)


def test_public_url_joins_and_strips_slashes(tmp_path: Path) -> None:
    s = _settings(tmp_path, "R2_ENDPOINT=x\nR2_ACCESS_KEY_ID=y\nR2_SECRET_ACCESS_KEY=z\n")
    # base has a trailing slash, key has a leading slash — exactly one slash between
    assert r2.public_url(s, "/silly_smiles/wallpapers/0001.mp4") == (
        "https://pub-abc.r2.dev/silly_smiles/wallpapers/0001.mp4"
    )
