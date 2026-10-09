"""Pin the headless screen-id navigation and Simulator environment contracts.

DECISIONS 2026-10-09 "Контракт screen-id": the launch argument is the only
mandatory headless entry point, the URL scheme is optional, and the worker pins
the simulator locale to the job's original-app language.
"""

from __future__ import annotations

from iosforge.providers import base
from iosforge.providers.base import SCREEN_NAV_CONTRACT, SIMULATOR_ENV_CONTRACT


def test_contracts_are_exported() -> None:
    assert "SCREEN_NAV_CONTRACT" in base.__all__
    assert "SIMULATOR_ENV_CONTRACT" in base.__all__


def test_launch_argument_is_the_only_mandatory_entry_point() -> None:
    assert "-screen-id <id>" in SCREEN_NAV_CONTRACT
    assert "only mandatory entry point" in SCREEN_NAV_CONTRACT


def test_url_scheme_is_optional() -> None:
    assert "iosforge://screen/<id> is optional" in SCREEN_NAV_CONTRACT


def test_headless_mode_requirements() -> None:
    for requirement in ("onboarding", "permission prompts", "fixtures", "error screen"):
        assert requirement in SCREEN_NAV_CONTRACT


def test_simulator_locale_follows_job_language() -> None:
    assert "locale/region/language" in SIMULATOR_ENV_CONTRACT
    assert "job's original-app language" in SIMULATOR_ENV_CONTRACT


def test_stable_frame_uses_tolerant_pixel_diff() -> None:
    assert "< 0.5 % of pixels" in SIMULATOR_ENV_CONTRACT
    assert "no byte hash" in SIMULATOR_ENV_CONTRACT
    assert "--terminate-running-process" in SIMULATOR_ENV_CONTRACT
