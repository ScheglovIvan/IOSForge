"""The Scope tab after a scope extension: the decision overlaid with the app as built."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from typing import Any

from iosforge.admin import scope_view
from iosforge.admin.templating import templates
from iosforge.mvp.scope_models import FeasibilityReport, ScopeCounts, ScopeDecision, ScreenScope
from iosforge.worker.swiftui_tasks import built_scope_key

_AT = datetime(2026, 10, 15, 9, 0, 0, tzinfo=UTC)


def _approved(mode: str = "core") -> ScopeDecision:
    screens = [
        ScreenScope(screen_id="0000", name="Home", include=True, reason="core"),
        ScreenScope(screen_id="0001", name="Meter", include=True, reason="core"),
        ScreenScope(screen_id="0002", name="History", include=False, reason="later"),
    ]
    return ScopeDecision(
        status="approved",
        scope_mode=mode,  # type: ignore[arg-type]
        screens=screens,
        feasibility=FeasibilityReport(overall_verdict="native", summary="ok"),
        counts=ScopeCounts.from_screens(screens),
        proposed_at=_AT,
        approved_at=_AT,
        approved_by="op",
    )


class _Storage:
    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = objects or {}

    def exists(self, key: str) -> bool:
        return key in self.objects

    def get(self, key: str, version_id: str | None = None) -> bytes:
        return self.objects[key]


def _requests(*screens: str) -> list[dict[str, Any]]:
    meta = scope_view.record_extension({}, list(screens), "op", _AT)
    return scope_view.extensions(meta)


def test_finished_extension_shows_the_added_screen_as_added_in_rework() -> None:
    view = scope_view.build(_approved(), ["0000", "0001", "0002"], _requests("0002"), running=False)
    assert view.statuses["0000"] == scope_view.ScreenStatus("built", "success")
    assert view.statuses["0002"] == scope_view.ScreenStatus("added in rework", "info")
    assert view.added == ["0002"] and view.pending == [] and view.unlisted == []


def test_queued_extension_is_shown_as_requested_until_the_round_builds_it() -> None:
    running = scope_view.build(_approved(), ["0000", "0001"], _requests("0002"), running=True)
    assert running.statuses["0002"].label == "requested · round running"
    assert running.pending == ["0002"] and running.added == []
    idle = scope_view.build(_approved(), ["0000", "0001"], _requests("0002"), running=False)
    assert idle.statuses["0002"] == scope_view.ScreenStatus("requested · not built", "warning")


def test_without_a_built_scope_only_requests_are_marked() -> None:
    view = scope_view.build(_approved(), None, [], running=False)
    assert view.built is None and view.statuses == {}


def test_full_mode_counts_every_screen_as_approved() -> None:
    view = scope_view.build(_approved("full"), ["0000", "0001", "0002"], [], running=False)
    assert {s.label for s in view.statuses.values()} == {"built"} and view.added == []


def test_screens_outside_the_proposal_are_listed_separately() -> None:
    view = scope_view.build(_approved(), ["0000", "0001", "0007"], _requests("9999"), running=False)
    assert view.unlisted == ["0007", "9999"] and view.added == ["0007"]
    assert view.statuses["0000"].label == "built" and "9999" in view.pending


def test_approved_screen_missing_from_the_app_is_flagged() -> None:
    view = scope_view.build(_approved(), ["0000"], [], running=False)
    assert view.statuses["0001"] == scope_view.ScreenStatus("not in the app", "danger")


def test_record_extension_keeps_the_operator_decision_bounded() -> None:
    meta: dict[str, Any] = {"scope_status": "approved", "scope_extensions": "garbage"}
    for i in range(scope_view.MAX_EXTENSIONS + 3):
        meta = scope_view.record_extension(meta, [f"{i:04d}", f"{i:04d}"], "op", _AT)
    entries = scope_view.extensions(meta)
    assert meta["scope_status"] == "approved" and len(entries) == scope_view.MAX_EXTENSIONS
    assert entries[-1] == {
        "screens": [f"{scope_view.MAX_EXTENSIONS + 2:04d}"],
        "by": "op",
        "at": _AT.isoformat(),
    }


def test_load_reads_the_built_scope_and_ignores_proposals() -> None:
    job_id = str(uuid.uuid4())
    storage = _Storage({built_scope_key(job_id): json.dumps({"screens": ["0000"]}).encode()})
    view = scope_view.load(storage, job_id, {}, _approved(), running=False)  # type: ignore[arg-type]
    assert view is not None and view.built == ["0000"]
    proposed = _approved().model_copy(update={"status": "proposed"})
    assert scope_view.load(storage, job_id, {}, proposed, running=False) is None  # type: ignore[arg-type]
    assert scope_view.load(storage, job_id, {}, None, running=False) is None  # type: ignore[arg-type]


def test_unreadable_built_scope_is_treated_as_unknown() -> None:
    job_id = str(uuid.uuid4())
    assert scope_view.read_built(_Storage(), job_id) is None  # type: ignore[arg-type]
    broken = _Storage({built_scope_key(job_id): b"{broken"})
    assert scope_view.read_built(broken, job_id) is None  # type: ignore[arg-type]


def test_scope_tab_renders_the_added_screens() -> None:
    meta = scope_view.record_extension({"scope_status": "approved"}, ["0002"], "op", _AT)
    job = NS(
        id="job-1",
        source_app_ref="Meter",
        state=NS(value="done"),
        created_at=_AT,
        source_app_metadata=meta,
        result_version=3,
    )
    scope = _approved()
    view = scope_view.build(
        scope, ["0000", "0001", "0002"], scope_view.extensions(meta), running=False
    )
    html = templates.env.get_template("job_detail.html").render(
        request=NS(url=NS(path="/jobs/job-1")),
        user=NS(username="op", csrf_token="t"),
        job=job,
        gen=None,
        build=None,
        can_ios_build=False,
        store_slides=[],
        signing={"configured": False, "own": False, "key_name": "", "key_id": "", "issuer_id": ""},
        signed_ok=False,
        sign_error="",
        timeline=[],
        codegen_tasks=[],
        scope_status="approved",
        scope=scope,
        scope_view=view,
    )
    assert "<th>In the app</th>" in html
    assert "added in rework" in html and "pill--info" in html
    assert "In the app now: <strong" in html and ">3</strong> screens" in html
    assert "Screens added by <strong" in html and "2026-10-15 09:00:00" in html
