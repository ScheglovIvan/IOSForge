"""Build artifacts of a job for the admin: original vs generated screens, judge, functional.

Everything comes from what the build already stored: the crawl (``WalkthroughResult``),
the Vision Judge report with its structural audit and functional run
(``GenerationResult.selftest_report``) and the simulator renders under
``jobs/<id>/generated_screenshots/``. Keys are derived, never listed; the browser fetches
each image lazily through the job-scoped object route.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from iosforge.common.logging import get_logger
from iosforge.db.models import GenerationResult, WalkthroughResult
from iosforge.storage.client import ArtifactStorage, build_key

log = get_logger("admin.artifacts")

NAMES_TTL_S = 60.0
NAMES_CACHE_MAX = 256
_NAMES: dict[str, tuple[float, dict[str, str]]] = {}
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")

STATUS_TONE = {
    "pass": "success",
    "soft_pass": "warning",
    "below_floor": "danger",
    "clone_risk": "danger",
}


@dataclass(frozen=True)
class ScreenRow:
    """One screen: the original capture next to the judged simulator render."""

    screen_id: str
    name: str
    original_key: str | None
    generated_key: str | None
    score: float | None = None
    divergence: float | None = None
    diffs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class JudgeSummary:
    """The Vision Judge's verdict on the whole app (last refine iteration)."""

    compliance_score: float | None
    structure: float | None
    coverage: float | None
    flows: float | None
    divergence: float | None
    status: str
    stop_reason: str
    iteration: int | None
    threshold: float | None
    version: int | None

    @property
    def tone(self) -> str:
        """Pill colour of the status."""
        return STATUS_TONE.get(self.status, "info")


@dataclass(frozen=True)
class CheckRow:
    """One functional check: the UI flow and the effects its module recorded."""

    name: str
    screen_id: str
    passed: bool
    ui_passed: bool
    infra_error: bool
    events: list[str]
    missing_events: list[str]
    flow: str
    message: str

    @property
    def status(self) -> str:
        """``passed`` / ``infra error`` / ``failed``."""
        if self.passed:
            return "passed"
        return "infra error" if self.infra_error else "failed"


@dataclass(frozen=True)
class FunctionalLog:
    """The functional run of the judged app (``functional_report.json``)."""

    ok: bool
    infra_error: bool
    skipped: str
    error: str
    checks: list[CheckRow]


@dataclass(frozen=True)
class ArtifactsView:
    """Everything the Artifacts tab shows."""

    built: bool
    screens: list[ScreenRow]
    judge: JudgeSummary | None
    functional: FunctionalLog | None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _strings(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _key(job_id: str, kind: str, name: str) -> str | None:
    try:
        return build_key(job_id=job_id, kind=kind, name=name)
    except ValueError:
        return None


def _originals(
    job_id: str, screen_map: dict[str, Any] | None, screenshot_keys: list[str]
) -> dict[str, str]:
    """Original screen id → its stored capture (only keys of this job)."""
    prefix = f"jobs/{job_id}/"
    by_name = {key.rsplit("/", 1)[-1]: key for key in screenshot_keys if key.startswith(prefix)}
    found: dict[str, str] = {}
    for screen in (screen_map or {}).get("screens", []) or []:
        if not isinstance(screen, dict) or "id" not in screen:
            continue
        shot = PurePosixPath(str(screen.get("screenshot") or "")).name
        if shot in by_name:
            found[str(screen["id"])] = by_name[shot]
    return found


def _crawl_ids(screen_map: dict[str, Any] | None) -> list[str]:
    return [
        str(s["id"])
        for s in (screen_map or {}).get("screens", []) or []
        if isinstance(s, dict) and "id" in s
    ]


def _judge(report: dict[str, Any]) -> JudgeSummary | None:
    if _float(report.get("compliance_score")) is None and not report.get("status"):
        return None
    return JudgeSummary(
        compliance_score=_float(report.get("compliance_score")),
        structure=_float(report.get("structure")),
        coverage=_float(report.get("coverage")),
        flows=_float(report.get("flows")),
        divergence=_float(report.get("divergence")),
        status=str(report.get("status") or ""),
        stop_reason=str(report.get("stop_reason") or ""),
        iteration=_int(report.get("iteration")),
        threshold=_float(report.get("threshold")),
        version=_int(report.get("version")),
    )


def _functional(report: dict[str, Any]) -> FunctionalLog | None:
    """The functional run: the refine loop nests it in ``structural``, a rework round
    (``compliance.verify_ios``) keeps it at the top level of the report."""
    structural = report.get("structural")
    raw = structural.get("functional") if isinstance(structural, dict) else None
    if not isinstance(raw, dict):
        raw = report.get("functional")
    if not isinstance(raw, dict):
        return None
    checks = [
        CheckRow(
            name=str(c.get("name") or ""),
            screen_id=str(c.get("screen_id") or ""),
            passed=bool(c.get("passed")),
            ui_passed=bool(c.get("ui_passed")),
            infra_error=bool(c.get("infra_error")),
            events=_strings(c.get("events")),
            missing_events=_strings(c.get("missing_events")),
            flow=str(c.get("flow") or ""),
            message=str(c.get("message") or ""),
        )
        for c in raw.get("checks", []) or []
        if isinstance(c, dict)
    ]
    return FunctionalLog(
        ok=bool(raw.get("ok")),
        infra_error=bool(raw.get("infra_error")),
        skipped=str(raw.get("skipped") or ""),
        error=str(raw.get("error") or ""),
        checks=checks,
    )


def build(
    job_id: str,
    *,
    screen_map: dict[str, Any] | None,
    screenshot_keys: list[str],
    report: dict[str, Any] | None,
    names: dict[str, str],
) -> ArtifactsView:
    """Assemble the tab from stored data (pure).

    Rows follow the judge's screens (the built scope) once there is a report; before a
    build, or for a report without per-screen scores, they follow the crawl and carry no
    generated render.
    """
    from iosforge.worker.swiftui_tasks import GENERATED_SCREENS_KIND

    originals = _originals(job_id, screen_map, screenshot_keys)
    judged = [s for s in (report or {}).get("screens", []) or [] if isinstance(s, dict)]
    rows: list[ScreenRow] = []
    if judged:
        for screen in judged:
            sid = str(screen.get("id") or "")
            rendered = PurePosixPath(str(screen.get("generated") or "")).name
            rows.append(
                ScreenRow(
                    screen_id=sid,
                    name=names.get(sid, ""),
                    original_key=originals.get(sid),
                    generated_key=(
                        _key(job_id, GENERATED_SCREENS_KIND, rendered) if rendered else None
                    ),
                    score=_float(screen.get("score")),
                    divergence=_float(screen.get("divergence")),
                    diffs=_strings(screen.get("diffs")),
                )
            )
    else:
        rows = [
            ScreenRow(
                screen_id=sid,
                name=names.get(sid, ""),
                original_key=originals.get(sid),
                generated_key=None,
            )
            for sid in _crawl_ids(screen_map)
        ]
    return ArtifactsView(
        built=report is not None,
        screens=rows,
        judge=_judge(report) if report else None,
        functional=_functional(report) if report else None,
    )


def _read_names(storage: ArtifactStorage, job_id: str) -> dict[str, str] | None:
    try:
        spec = json.loads(
            storage.get(build_key(job_id=job_id, kind="app_spec", name="app_spec.json"))
        )
    except Exception as exc:
        log.info("admin.artifacts.no_spec", job_id=job_id, error=str(exc))
        return None
    screens = spec.get("screens", []) if isinstance(spec, dict) else []
    return {
        str(s["id"]): str(s.get("name") or "") for s in screens if isinstance(s, dict) and "id" in s
    }


def screen_names(storage: ArtifactStorage, job_id: str) -> dict[str, str]:
    """Screen id → human name from the job's ``app_spec.json`` (empty when unreadable).

    The job page reloads every few seconds while a build runs; the names are kept in
    process for :data:`NAMES_TTL_S` so each reload does not fetch the spec again. An
    unreadable spec is not cached, so names show up as soon as the spec exists.
    """
    now = time.monotonic()
    cached = _NAMES.get(job_id)
    if cached is not None and cached[0] > now:
        return cached[1]
    names = _read_names(storage, job_id)
    if names is None:
        return {}
    if len(_NAMES) >= NAMES_CACHE_MAX:
        _NAMES.clear()
    _NAMES[job_id] = (now + NAMES_TTL_S, names)
    return names


def load(
    db: Session, storage: ArtifactStorage, job_id: uuid.UUID, gen: GenerationResult | None
) -> ArtifactsView:
    """The Artifacts tab of a job from its crawl, its build report and its spec names."""
    walk = db.scalar(select(WalkthroughResult).where(WalkthroughResult.job_id == job_id))
    return build(
        str(job_id),
        screen_map=walk.screen_map if walk else None,
        screenshot_keys=list(walk.screenshot_keys or []) if walk else [],
        report=dict(gen.selftest_report or {}) if gen else None,
        names=screen_names(storage, str(job_id)) if walk or gen else {},
    )


def owned_key(job_id: uuid.UUID, key: str) -> bool:
    """True only for a plain object key under this job's prefix.

    A key of another job, or one that could be normalised out of the prefix (``..``,
    empty segments, backslashes, control characters), is refused so a crafted ``key``
    never reads someone else's artifacts.
    """
    prefix = f"jobs/{job_id}/"
    if not key.startswith(prefix) or len(key) > 1024:
        return False
    rest = key[len(prefix) :]
    if not rest or "\\" in rest or any(ord(ch) < 32 for ch in rest):
        return False
    return all(part not in ("", ".", "..") for part in rest.split("/"))


def content_disposition(disposition: str, filename: str) -> str:
    """A header-safe ``Content-Disposition``: ASCII fallback plus RFC 5987 ``filename*``."""
    fallback = _UNSAFE_FILENAME.sub("_", filename) or "download"
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"
