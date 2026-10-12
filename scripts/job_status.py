"""Print a job's state, timeline and builds from the local DB.

Usage: uv run python scripts/job_status.py <job_id>
"""

from __future__ import annotations

import sys
import uuid

from sqlalchemy import select

from iosforge.db.models import GenerationResult, Job, StageTimeline, XcodeBuild
from iosforge.db.session import get_sessionmaker


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    try:
        job_id = uuid.UUID(argv[1])
    except ValueError:
        print(f"not a job id: {argv[1]}", file=sys.stderr)
        return 2
    with get_sessionmaker()() as db:
        job = db.get(Job, job_id)
        if job is None:
            print(f"job not found: {job_id}", file=sys.stderr)
            return 1
        meta = job.source_app_metadata or {}
        print(
            "state:",
            job.state.value,
            "| scope:",
            meta.get("scope_status"),
            "| v",
            job.result_version,
        )
        timeline = db.scalars(
            select(StageTimeline)
            .where(StageTimeline.job_id == job_id)
            .order_by(StageTimeline.started_at)
        )
        for row in timeline:
            if row.duration_ms:
                dur = f"{row.duration_ms // 1000}s"
            else:
                dur = "…" if not row.finished_at else ""
            err = f" ERR {row.error[:160]}" if row.error else ""
            print(f"  {row.stage.value:<12} {dur}{err}")
        gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job_id))
        if gen:
            print("gen:", gen.sources_key, "score", gen.compliance_score)
        for b in db.scalars(select(XcodeBuild).where(XcodeBuild.job_id == job_id)):
            message = (b.message or "")[:200]
            print("xcode:", b.status, b.signed, b.build_number, b.ipa_key, message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
