"""Print a job's state, timeline and builds from the local DB.

Usage: uv run python scripts/job_status.py <job_id>
"""

import sys
import uuid

from sqlalchemy import select

from iosforge.db.models import GenerationResult, Job, StageTimeline, XcodeBuild
from iosforge.db.session import get_sessionmaker

job_id = uuid.UUID(sys.argv[1])
with get_sessionmaker()() as db:
    job = db.get(Job, job_id)
    meta = job.source_app_metadata or {}
    print("state:", job.state.value, "| scope:", meta.get("scope_status"), "| v", job.result_version)
    for row in db.scalars(
        select(StageTimeline).where(StageTimeline.job_id == job_id).order_by(StageTimeline.started_at)
    ):
        dur = f"{row.duration_ms // 1000}s" if row.duration_ms else ("…" if not row.finished_at else "")
        err = f" ERR {row.error[:160]}" if row.error else ""
        print(f"  {row.stage.value:<12} {dur}{err}")
    gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job_id))
    if gen:
        print("gen:", gen.sources_key, "score", gen.compliance_score)
    for b in db.scalars(select(XcodeBuild).where(XcodeBuild.job_id == job_id)):
        print("xcode:", b.status, b.signed, b.build_number, b.ipa_key, (b.message or "")[:200])
