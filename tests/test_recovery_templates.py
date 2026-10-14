"""Job page pieces of job recovery: buttons, flashes, muted operator rows, scope banner."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace as NS
from typing import Any

from iosforge.admin.templating import templates
from tests.test_recovery import _approved_scope

_NOW = datetime(2026, 10, 15, 9, 30, 0)
_USER = NS(username="op", csrf_token="csrf-tok")


def _job(state: str, **meta: Any) -> NS:
    return NS(
        id="job-9",
        source_app_ref="Dogo",
        state=NS(value=state),
        created_at=_NOW,
        source_app_metadata=meta,
        result_version=1,
    )


def _render(name: str, **ctx: Any) -> str:
    ctx.setdefault("request", NS(url=NS(path="/jobs/job-9")))
    return templates.env.get_template(name).render(**ctx)


def _detail(job: NS, **ctx: Any) -> str:
    base: dict[str, Any] = {
        "user": _USER,
        "job": job,
        "gen": None,
        "build": None,
        "can_ios_build": False,
        "store_slides": [],
        "signing": {"configured": False},
        "signed_ok": False,
        "sign_error": "",
        "timeline": [],
        "codegen_tasks": [],
    }
    return _render("job_detail.html", **{**base, **ctx})


def test_buttons_are_hidden_by_default() -> None:
    html = _detail(_job("codegen"))
    assert "/jobs/job-9/reset" not in html and "/jobs/job-9/scope/reopen" not in html


def test_reset_button_has_csrf_and_confirm() -> None:
    html = _detail(_job("codegen"), can_reset=True, last_activity_s=1800)
    form = html[html.index('action="/jobs/job-9/reset"') :]
    form = form[: form.index("</form>")]
    assert "confirm(" in form and "30 min ago" in form
    assert 'name="csrf_token" value="csrf-tok"' in form
    assert "Reset stuck job" in form


def test_reopen_button_in_header_and_approved_scope_view() -> None:
    html = _detail(
        _job("failed", scope_status="approved"),
        can_reopen=True,
        scope_status="approved",
        scope=_approved_scope(),
    )
    assert html.count('action="/jobs/job-9/scope/reopen"') == 2
    assert html.count("Re-open the scope of this failed job?") == 2
    assert 'id="t-scope"' in html


def test_flash_messages() -> None:
    html = _detail(_job("queued"), reset_to="queued")
    assert "Job reset by an operator" in html and "<strong>queued</strong>" in html
    html = _detail(_job("needs_input", scope_status="proposed"), reopened_ok=True)
    assert "Scope re-opened with the previous selection kept" in html


def test_operator_rows_are_muted_and_failures_stay_red() -> None:
    timeline = [
        NS(
            stage=NS(value="codegen"),
            started_at=_NOW,
            finished_at=_NOW,
            duration_ms=10,
            error="codegen failed: screens reachable from no tab: ['0002']",
        ),
        NS(
            stage=NS(value="scope"),
            started_at=_NOW,
            finished_at=_NOW,
            duration_ms=0,
            error="operator: scope re-opened by op: failed → needs_input",
        ),
    ]
    html = _render(
        "_timeline.html", job=_job("needs_input"), timeline=timeline, codegen_tasks=[], gen=None
    )
    split = html.index('class="timeline-operator muted"')
    failure_row = html[:split]
    operator_row = html[split:]
    assert "var(--danger-text)" in failure_row[failure_row.rindex("<tr>") :]
    operator_row = operator_row[: operator_row.index("</tr>")]
    assert "var(--danger-text)" not in operator_row
    assert '<td class="mono muted">operator: scope re-opened' in operator_row


def test_needs_input_banner_follows_the_scope_status() -> None:
    proposed = _render(
        "_timeline.html",
        job=_job("needs_input", scope_status="proposed"),
        timeline=[],
        codegen_tasks=[],
        gen=None,
    )
    assert "Scope awaits approval" in proposed
    assert "structural web verification" not in proposed
    held = _render(
        "_timeline.html", job=_job("needs_input"), timeline=[], codegen_tasks=[], gen=None
    )
    assert "Scope awaits approval" not in held and "structural web verification" in held
