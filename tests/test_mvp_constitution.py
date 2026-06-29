"""Tests for the deterministic development-constitution renderer."""

from __future__ import annotations

from typing import Any

from iosforge.mvp import constitution


def _spec(*, backend: bool = False) -> dict[str, Any]:
    return {
        "app_name": "Todo",
        "app_type": "productivity",
        "one_liner": "A todo app",
        "screens": [
            {"id": "0000", "name": "Home", "route": "/home"},
            {"id": "0001", "name": "Add"},
        ],
        "content": {"persistence": "local", "data_model": []},
        "backend": {"backend_needed": backend},
    }


def test_constitution_fixes_stack_and_ios_target() -> None:
    text = constitution.render_constitution(_spec())
    assert "Development Constitution" in text
    assert "iOS" in text and "Codemagic" in text
    assert "Riverpod" in text and "go_router" in text
    assert "tokens.json" in text


def test_constitution_states_fidelity_rule() -> None:
    text = constitution.render_constitution(_spec())
    assert "1:1" in text
    assert "iOS conventions win" in text


def test_constitution_lists_routes_from_screens() -> None:
    text = constitution.render_constitution(_spec())
    assert "`/home`" in text  # explicit route kept
    assert "`/0001`" in text  # fallback route derived from screen id


def test_constitution_has_parallel_build_discipline() -> None:
    text = constitution.render_constitution(_spec())
    assert "worktree" in text
    assert "lib/features/" in text  # screen workers own only their feature dir


def test_constitution_backend_rule_is_conditional() -> None:
    with_backend = constitution.render_constitution(_spec(backend=True))
    assert "needs a backend" in with_backend
    assert "openapi.yaml" in with_backend

    without = constitution.render_constitution(_spec(backend=False))
    assert "does not need a backend" in without
