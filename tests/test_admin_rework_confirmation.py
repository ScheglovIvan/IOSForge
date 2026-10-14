"""Apply rework / Add screens confirmation: flash, in-progress state, readable refusals."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text, update
from sqlalchemy.orm import Session

from iosforge.admin import csrf, rework_view, routes_jobs
from iosforge.admin.session import SessionData
from iosforge.common.types import JobState
from iosforge.db.base import Base
from iosforge.db.models import GenerationResult, Job, XcodeBuild
from iosforge.orchestrator import rework_request
from iosforge.worker import swiftui_rework
from iosforge.worker.swiftui_tasks import latest_xcode_build, save_built_scope, sources_key
from tests.test_recovery import _DB_URL, _settings, _Storage, needs_db
from tests.test_recovery_templates import _detail, _job

_NOW = datetime(2026, 10, 15, 9, 30, 0, tzinfo=UTC)
SPEC_KEY = "jobs/{}/app_spec/app_spec.json"
_PROBLEM_TEXT = "Header overflows on Settings; paywall Continue does nothing"


def _status(
    meta: dict[str, Any], *, running: bool = False, waited: timedelta = timedelta(seconds=30)
) -> rework_view.ReworkRound:
    return rework_view.status(meta, codegen_running=running, now=_NOW + waited, stall_after_s=300)


def _requested(instructions: str = "fix it", screens: list[str] | None = None) -> dict[str, Any]:
    summary = rework_view.summarize(instructions, screens)
    meta, _ = rework_request.new_request({"app_id": "42"}, summary=summary, actor="anna", at=_NOW)
    return meta


def _marked(meta: dict[str, Any], **fields: object) -> dict[str, Any]:
    updated = rework_request.mark(meta, meta[rework_request.REQUEST_KEY]["id"], **fields)
    assert updated is not None
    return updated


def test_summary_lists_screens_and_quotes_instructions() -> None:
    assert rework_view.summarize("  fix\n the  header ", ["0009", "0012"]) == (
        "add screens 0009, 0012; “fix the header”"
    )
    long = rework_view.summarize("x" * 400, None)
    assert len(long) <= rework_view.INSTRUCTIONS_PREVIEW + 2 and long.endswith("…”")
    many = rework_view.summarize("", [f"{i:04d}" for i in range(15)])
    assert many.endswith("(+3 more)")


def test_request_ids_supersede_older_messages() -> None:
    first = _requested()
    old_id = first[rework_request.REQUEST_KEY]["id"]
    second, new_id = rework_request.new_request(first, summary="", actor="op", at=_NOW)
    assert new_id != old_id and len(new_id) == 32
    assert rework_request.is_stale(second, old_id) and not rework_request.is_stale(second, new_id)
    assert not rework_request.is_stale(second, None) and rework_request.is_stale({}, new_id)
    assert rework_request.mark(second, old_id, started=True) is None
    assert rework_request.withdraw(second, old_id) is None
    assert rework_request.withdraw(second, new_id) == {"app_id": "42"}
    legacy = {rework_request.REQUEST_KEY: {"summary": "x", "at": _NOW.isoformat()}}
    assert rework_request.current(legacy) is None and _status(legacy).phase is None


def test_round_phases_come_from_the_request_record() -> None:
    meta = _requested()
    assert meta["app_id"] == "42" and meta[rework_request.REQUEST_KEY]["by"] == "anna"
    assert _status({}).phase is None
    queued = _status(meta)
    assert queued.phase == "queued" and queued.busy and queued.waited_s == 30
    assert queued.summary == "“fix it”"
    stalled = _status(meta, waited=timedelta(minutes=6))
    assert stalled.phase == "stalled" and not stalled.busy
    started = _marked(meta, started=True)
    running = _status(started, running=True)
    assert running.phase == "running" and running.summary == "“fix it”"
    assert _status(started).phase is None
    assert _status({}, running=True) == rework_view.ReworkRound("running")
    assert _status(meta, running=True).summary == ""
    declined = _status(_marked(meta, declined="no simulator"), waited=timedelta(hours=1))
    assert declined.phase == "declined" and declined.reason == "no simulator"
    assert not declined.busy


def _round(phase: rework_view.Phase | None, summary: str = "“fix it”") -> rework_view.ReworkRound:
    return rework_view.ReworkRound(phase, summary, "anna", 120)


def _swiftui_page(state: str = "done", **ctx: Any) -> str:
    gen = NS(sources_key="jobs/x/sources/xcode_app.zip", selftest_report={}, github_repo_url=None)
    return _detail(_job(state), gen=gen, **ctx)


def _rework_form(html: str) -> str:
    form = html[html.index('action="/jobs/job-9/rework"') :]
    return form[: form.index("</form>")]


def test_flash_after_queueing_names_the_round() -> None:
    html = _swiftui_page(rework_queued=True, rework_round=_round("queued"))
    assert (
        "Rework round queued: “fix it”. The page will update when the Mac worker picks it up."
        in (html)
    )
    assert 'http-equiv="refresh" content="8; url=/jobs/job-9"' in html


def test_rework_tab_shows_the_round_in_progress() -> None:
    queued = _swiftui_page(rework_round=_round("queued"))
    assert "Rework round in progress</strong> — queued by anna 2 min ago: “fix it”" in queued
    assert "disabled" in _rework_form(queued) and "Pipeline is busy" not in queued
    assert 'http-equiv="refresh"' in queued
    running = _swiftui_page("codegen", rework_round=_round("running"))
    assert "Rework round in progress</strong>: “fix it”" in running
    assert "disabled" in _rework_form(running)
    stalled = _swiftui_page(rework_round=_round("stalled"))
    assert "Queued 2 min ago and not started yet: “fix it”" in stalled
    assert "Queue it again to replace the waiting request; the old one will be skipped." in stalled
    assert "disabled" not in _rework_form(stalled) and 'http-equiv="refresh"' not in stalled
    declined = _swiftui_page(rework_round=rework_view.ReworkRound("declined", "x", reason="no sim"))
    assert "The Mac worker did not run the last round (x): no sim." in declined
    assert "disabled" not in _rework_form(declined)
    idle = _swiftui_page(rework_round=_round(None))
    assert "rework-status" not in idle and "disabled" not in _rework_form(idle)


def test_refusal_page_keeps_the_input_on_the_rework_tab() -> None:
    html = _swiftui_page(
        rework_round=_round(None),
        xcode_build=NS(status="queued", message=""),
        rework_error="Rework is not possible now: a native delivery is in flight.",
        rework_draft=_PROBLEM_TEXT + " <b>",
        screens_draft="0009, 0012",
    )
    assert "a native delivery is in flight. Your input is kept on the Rework tab." in html
    assert _PROBLEM_TEXT + " &lt;b&gt;</textarea>" in html
    assert 'value="0009, 0012"' in html
    assert 'data-tab-forced="t-rework"' in html
    assert 'id="t-rework" checked' in html
    assert 'http-equiv="refresh"' not in html


@pytest.fixture()
def engine() -> Iterator[Engine]:
    schema = f"t3e_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_DB_URL)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    eng = create_engine(_DB_URL, execution_options={"schema_translate_map": {None: schema}})
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()
        admin = create_engine(_DB_URL)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


_SPEC = {"screens": [{"id": f"{i:04d}", "name": f"S{i}"} for i in range(13)]}


def _make_job(
    eng: Engine,
    storage: _Storage,
    state: JobState = JobState.DONE,
    *,
    meta: dict[str, Any] | None = None,
    delivery: bool = False,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    key = sources_key(str(job_id))
    storage.put(key, b"zip")
    storage.put(SPEC_KEY.format(job_id), json.dumps(_SPEC).encode())
    save_built_scope(storage, str(job_id), ["0000", "0001"])  # type: ignore[arg-type]
    with Session(eng) as db:
        db.add(
            Job(
                id=job_id,
                source_app_ref="Dogo",
                state=state,
                source_app_metadata={"app_id": "42", **(meta or {})},
            )
        )
        db.flush()
        db.add(GenerationResult(job_id=job_id, sources_key=key, selftest_report={}))
        if delivery:
            db.add(XcodeBuild(job_id=job_id, status="queued", started_at=datetime.now(UTC)))
        db.commit()
    return job_id


def _page_context(
    request: Any, job: Job, user: Any, db: Session, storage: Any, **_k: Any
) -> dict[str, Any]:
    gen = NS(sources_key=sources_key(str(job.id)), selftest_report={}, github_repo_url=None)
    signing = {"configured": False, "own": False, "key_name": "", "key_id": "", "issuer_id": ""}
    return {
        "job": job,
        "user": user,
        "gen": gen,
        "build": None,
        "xcode_build": latest_xcode_build(db, job.id),
        "can_ios_build": False,
        "store_slides": [],
        "signing": signing,
        "signed_ok": False,
        "sign_error": "",
        "timeline": [],
        "codegen_tasks": [],
        "rework_round": routes_jobs._rework_round(job, True),
        "rework_queued": False,
        "rework_error": "",
        "rework_draft": "",
        "screens_draft": "",
    }


def _wire(
    monkeypatch: pytest.MonkeyPatch, *, on_send: Callable[[], None] | None = None
) -> list[Any]:
    sent: list[Any] = []

    def _apply_async(args: list[Any], kwargs: dict[str, Any], queue: str) -> None:
        if on_send is not None:
            on_send()
        sent.append((args, kwargs, queue))

    monkeypatch.setattr(routes_jobs, "get_settings", _settings)
    monkeypatch.setattr(routes_jobs, "_detail_context", _page_context)
    monkeypatch.setattr(swiftui_rework.rework_swiftui, "apply_async", _apply_async)
    return sent


def _user() -> SessionData:
    return SessionData(user_id="1", username="anna", csrf_token=csrf.new_token())


def _rework(eng: Engine, storage: _Storage, job_id: uuid.UUID, instructions: str) -> Any:
    user = _user()
    with Session(eng) as db:
        return routes_jobs.jobs_rework(
            request=NS(url=NS(path=f"/jobs/{job_id}/rework")),  # type: ignore[arg-type]
            job_id=job_id,
            csrf_token=user.csrf_token,
            instructions=instructions,
            user=user,
            db=db,
            storage=storage,  # type: ignore[arg-type]
        )


def _extend(eng: Engine, storage: _Storage, job_id: uuid.UUID, screens: str) -> Any:
    user = _user()
    with Session(eng) as db:
        return routes_jobs.jobs_extend_scope(
            request=NS(url=NS(path=f"/jobs/{job_id}/extend-scope")),  # type: ignore[arg-type]
            job_id=job_id,
            csrf_token=user.csrf_token,
            screens=screens,
            user=user,
            db=db,
            storage=storage,  # type: ignore[arg-type]
        )


def _meta(eng: Engine, job_id: uuid.UUID) -> dict[str, Any]:
    with Session(eng) as db:
        job = db.get(Job, job_id)
        assert job is not None
        return dict(job.source_app_metadata)


def _set_meta(eng: Engine, job_id: uuid.UUID, meta: dict[str, Any]) -> None:
    with eng.begin() as conn:
        conn.execute(update(Job).where(Job.id == job_id).values(source_app_metadata=meta))


def _request(eng: Engine, job_id: uuid.UUID) -> dict[str, Any]:
    entry = rework_request.current(_meta(eng, job_id))
    assert entry is not None
    return entry


@needs_db
def test_apply_rework_redirects_with_the_flash_and_records_the_round(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _wire(monkeypatch)
    storage = _Storage()
    job_id = _make_job(engine, storage)

    resp = _rework(engine, storage, job_id, f"  {_PROBLEM_TEXT}  ")

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/jobs/{job_id}?rework=queued#t-rework"
    request = _request(engine, job_id)
    assert [s[0] for s in sent] == [[str(job_id), _PROBLEM_TEXT, None]]
    assert [s[1] for s in sent] == [{"epoch": 0, "request_id": request["id"]}]
    assert request["by"] == "anna" and request["summary"] == f"“{_PROBLEM_TEXT}”"
    with Session(engine) as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert routes_jobs._rework_round(job, True).phase == "queued"


@needs_db
def test_add_screens_redirects_with_the_flash_to_the_scope_tab(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _wire(monkeypatch)
    storage = _Storage()
    approved = _make_job(engine, storage, meta={"scope_status": "approved"})
    bare = _make_job(engine, storage)

    resp = _extend(engine, storage, approved, "0009, 0012")
    assert resp.headers["location"] == f"/jobs/{approved}?rework=queued#t-scope"
    meta = _meta(engine, approved)
    assert meta[rework_request.REQUEST_KEY]["summary"] == "add screens 0009, 0012"
    assert meta["scope_extensions"][-1]["screens"] == ["0009", "0012"]
    assert _extend(engine, storage, bare, "0009").headers["location"] == (
        f"/jobs/{bare}?rework=queued#t-rework"
    )
    assert [s[0][2] for s in sent] == [["0009", "0012"], ["0009"]]


@needs_db
def test_add_screens_refuses_unknown_or_built_ids_on_the_page(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _wire(monkeypatch)
    storage = _Storage()
    job_id = _make_job(engine, storage, meta={"scope_status": "approved"})
    before = _meta(engine, job_id)

    unknown = _extend(engine, storage, job_id, "0009, 9999 abc")
    built = _extend(engine, storage, job_id, "0000 0001")

    assert (unknown.status_code, built.status_code, sent) == (409, 409, [])
    body = unknown.body.decode()
    assert "unknown screen ids 9999, abc; use the ids of the full app spec." in body
    assert 'value="0009, 9999 abc"' in body
    assert "every listed screen is already in the app." in built.body.decode()
    assert _meta(engine, job_id) == before


@needs_db
def test_a_second_submit_while_queued_is_refused_on_the_page(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _wire(monkeypatch)
    storage = _Storage()
    job_id = _make_job(engine, storage)
    _rework(engine, storage, job_id, "first")

    resp = _rework(engine, storage, job_id, _PROBLEM_TEXT)

    assert resp.status_code == 409 and len(sent) == 1
    body = resp.body.decode()
    assert "Rework is not possible now: a rework round is already queued." in body
    assert f"{_PROBLEM_TEXT}</textarea>" in body
    assert "Rework round in progress" in body
    meta = _meta(engine, job_id)
    started = rework_request.mark(meta, meta[rework_request.REQUEST_KEY]["id"], started=True)
    assert started is not None
    _set_meta(engine, job_id, started)
    assert _rework(engine, storage, job_id, "again").status_code == 303


@needs_db
def test_requeueing_a_stalled_round_skips_the_waiting_message(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _wire(monkeypatch)
    storage = _Storage()
    job_id = _make_job(engine, storage)
    _rework(engine, storage, job_id, "first")
    meta = _meta(engine, job_id)
    old_at = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    meta[rework_request.REQUEST_KEY] = {**meta[rework_request.REQUEST_KEY], "at": old_at}
    _set_meta(engine, job_id, meta)
    with Session(engine) as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert routes_jobs._rework_round(job, True).phase == "stalled"

    assert _rework(engine, storage, job_id, "second").status_code == 303

    old_id, new_id = (s[1]["request_id"] for s in sent)
    assert old_id != new_id and _request(engine, job_id)["id"] == new_id
    monkeypatch.setattr(swiftui_rework, "get_sessionmaker", lambda: lambda: Session(engine))
    monkeypatch.setattr(swiftui_rework, "S3ArtifactStorage", lambda: storage)
    monkeypatch.setattr(swiftui_rework, "get_settings", _settings)

    def _no_mac() -> None:
        raise swiftui_rework.simulator.SimulatorUnavailable("needs a Mac worker")

    monkeypatch.setattr(swiftui_rework.simulator, "require_toolchain", _no_mac)
    before = _meta(engine, job_id)
    out = swiftui_rework.rework_swiftui.run(str(job_id), "first", None, 0, request_id=old_id)
    assert "stale rework request" in out and _meta(engine, job_id) == before
    out = swiftui_rework.rework_swiftui.run(str(job_id), "second", None, 0, request_id=new_id)
    assert "needs a Mac worker" in out
    assert _request(engine, job_id)["declined"] == "needs a Mac worker"
    with Session(engine) as db:
        job = db.get(Job, job_id)
        assert job is not None and job.state == JobState.DONE
        declined = routes_jobs._rework_round(job, True)
        assert declined.phase == "declined" and not declined.busy


@needs_db
def test_a_delivery_in_flight_refusal_is_readable_and_keeps_the_input(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _wire(monkeypatch)
    storage = _Storage()
    job_id = _make_job(engine, storage, delivery=True)
    before = _meta(engine, job_id)

    resp = _extend(engine, storage, job_id, "0009 0012")

    assert resp.status_code == 409 and sent == []
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.body.decode()
    assert "Rework is not possible now: a native delivery is in flight." in body
    assert 'value="0009 0012"' in body and 'data-tab-forced="t-rework"' in body
    assert _meta(engine, job_id) == before


@needs_db
def test_a_broker_failure_withdraws_only_this_submission(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _Storage()
    earlier = {"screens": ["0005"], "by": "op", "at": "2026-10-01T09:00:00+00:00"}
    job_id = _make_job(
        engine, storage, meta={"scope_status": "approved", "scope_extensions": [earlier]}
    )

    def _reset_meanwhile_then_fail() -> None:
        meta = _meta(engine, job_id)
        _set_meta(engine, job_id, {**meta, "run_epoch": 5})
        raise ConnectionError("redis down")

    _wire(monkeypatch, on_send=_reset_meanwhile_then_fail)

    resp = _extend(engine, storage, job_id, "0009")

    assert resp.status_code == 503
    assert "the task broker did not accept it" in resp.body.decode()
    meta = _meta(engine, job_id)
    assert meta["run_epoch"] == 5 and rework_request.REQUEST_KEY not in meta
    assert meta["scope_extensions"] == [earlier]
