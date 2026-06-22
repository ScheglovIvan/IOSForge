"""DB layer tests (T-2.2, SPEC §10).

Covers:
* the Alembic baseline migration applies on a clean database (into an isolated
  throwaway schema) and downgrades cleanly;
* round-trip CRUD for every model;
* ``Job.state`` round-trips through the canonical :class:`JobState` enum.

Isolation: each run uses a uniquely-named PostgreSQL schema that is created in
``setup`` and dropped in ``teardown``, so the public schema is never touched and
parallel agents are unaffected. Requires the live Postgres from docker compose
and a psycopg driver (``uv run --with 'psycopg[binary]'``).
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session

from iosforge.common.config import get_settings
from iosforge.common.types import JobState, Stage
from iosforge.db.base import Base
from iosforge.db.models import (
    ApkArtifact,
    AuditLog,
    Candidate,
    GenerationResult,
    Job,
    PromptSet,
    PromptVersion,
    ProviderConfig,
    SourceConfig,
    StageTimeline,
    WalkthroughResult,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Skip the whole module when neither Postgres nor a driver is available, so the
# suite stays green in environments without the infra (the CI/local run that owns
# this task provides both).
try:  # pragma: no cover - import guard
    import psycopg  # noqa: F401

    _HAS_DRIVER = True
except ImportError:  # pragma: no cover
    _HAS_DRIVER = False


def _db_reachable(url: str) -> bool:
    try:
        eng = create_engine(url)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:  # pragma: no cover - infra missing
        return False


_DB_URL = get_settings().database_url
pytestmark = pytest.mark.skipif(
    not (_HAS_DRIVER and _db_reachable(_DB_URL)),
    reason="requires live Postgres + psycopg driver",
)


@pytest.fixture()
def schema_engine() -> Iterator[tuple[Engine, str]]:
    """An engine whose default schema is a throwaway, per-test schema."""
    schema = f"t2_2_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_DB_URL)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()

    engine = create_engine(
        _DB_URL,
        execution_options={"schema_translate_map": {None: schema}},
    )
    # Build all tables (and native enum types) inside the isolated schema.
    Base.metadata.create_all(engine)

    try:
        yield engine, schema
    finally:
        engine.dispose()
        admin = create_engine(_DB_URL)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_migration_applies_and_downgrades_on_clean_db() -> None:
    """`alembic upgrade head` then `downgrade base` run cleanly in a fresh schema."""
    schema = f"mig_{uuid.uuid4().hex[:12]}"
    env = {**os.environ, "IOSFORGE_DB_SCHEMA": schema}

    def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["alembic", *args],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

    up = _alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    # Verify the expected tables now exist in the isolated schema.
    engine = create_engine(_DB_URL)
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names(schema=schema))
        expected = {
            "jobs",
            "candidates",
            "apk_artifacts",
            "walkthrough_results",
            "generation_results",
            "prompt_sets",
            "prompt_versions",
            "provider_configs",
            "source_configs",
            "audit_logs",
            "stage_timeline",
        }
        assert expected <= tables, expected - tables

        down = _alembic("downgrade", "base")
        assert down.returncode == 0, down.stderr
        insp = inspect(engine)
        remaining = set(insp.get_table_names(schema=schema)) - {"alembic_version"}
        assert remaining == set(), remaining
    finally:
        engine.dispose()
        admin = create_engine(_DB_URL)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def test_job_crud_roundtrip_with_enum_state(schema_engine: tuple[Engine, str]) -> None:
    """A Job round-trips and Job.state deserialises back into JobState."""
    engine, _ = schema_engine
    with Session(engine) as session:
        job = Job(
            source_app_ref="https://apps.apple.com/app/id123",
            source_app_metadata={"name": "Demo", "publisher": "Acme"},
            state=JobState.DISCOVERY,
            emulator_provider="firebase",
        )
        session.add(job)
        session.commit()
        job_id = job.id

    with Session(engine) as session:
        loaded = session.get(Job, job_id)
        assert loaded is not None
        assert loaded.source_app_metadata == {"name": "Demo", "publisher": "Acme"}
        assert loaded.state is JobState.DISCOVERY
        assert isinstance(loaded.state, JobState)
        assert isinstance(loaded.created_at, dt.datetime)
        assert loaded.created_at.tzinfo is not None  # timezone-aware


def test_child_models_crud_and_cascade(schema_engine: tuple[Engine, str]) -> None:
    """Candidate / ApkArtifact / Walkthrough / Generation round-trip and cascade."""
    engine, _ = schema_engine
    with Session(engine) as session:
        job = Job(source_app_ref="ios://app", state=JobState.QUEUED)
        job.candidates.append(
            Candidate(package_id="com.acme.app", score=0.91, selected=True)
        )
        job.apk_artifacts.append(
            ApkArtifact(storage_key="apk/job/1.apk", sha256="ab" * 32, size_bytes=1024)
        )
        job.walkthrough_results.append(
            WalkthroughResult(
                screenshot_keys=["s/1.png", "s/2.png"],
                screen_map={"nodes": [1, 2], "edges": [[1, 2]]},
                provider="firebase",
                strategy={"max_screens": 40},
            )
        )
        job.generation_results.append(
            GenerationResult(
                sources_key="gen/job/src.zip",
                compliance_score=0.96,
                selftest_report={"passed": True},
            )
        )
        job.timeline.append(
            StageTimeline(stage=Stage.WALKTHROUGH, input_hash="deadbeef", duration_ms=500)
        )
        session.add(job)
        session.commit()
        job_id = job.id

    with Session(engine) as session:
        loaded = session.get(Job, job_id)
        assert loaded is not None
        assert loaded.candidates[0].selected is True
        assert loaded.apk_artifacts[0].size_bytes == 1024
        assert loaded.walkthrough_results[0].screenshot_keys == ["s/1.png", "s/2.png"]
        assert loaded.walkthrough_results[0].screen_map["edges"] == [[1, 2]]
        assert loaded.generation_results[0].compliance_score == 0.96
        assert loaded.timeline[0].stage is Stage.WALKTHROUGH

        # Deleting the Job cascades to all children.
        session.delete(loaded)
        session.commit()

    with Session(engine) as session:
        assert session.get(Job, job_id) is None
        assert session.query(Candidate).count() == 0
        assert session.query(ApkArtifact).count() == 0
        assert session.query(WalkthroughResult).count() == 0
        assert session.query(GenerationResult).count() == 0
        assert session.query(StageTimeline).count() == 0


def test_prompt_set_and_versions_roundtrip(schema_engine: tuple[Engine, str]) -> None:
    """PromptSet with multiple versions round-trips (SPEC §6)."""
    engine, _ = schema_engine
    with Session(engine) as session:
        ps = PromptSet(name="analysis", purpose="analysis", description="screen analysis")
        ps.versions.append(PromptVersion(version=1, body="v1 body", author="op"))
        ps.versions.append(PromptVersion(version=2, body="v2 body", author="op"))
        session.add(ps)
        session.commit()
        ps_id = ps.id

    with Session(engine) as session:
        loaded = session.get(PromptSet, ps_id)
        assert loaded is not None
        assert loaded.purpose == "analysis"
        bodies = sorted(v.body for v in loaded.versions)
        assert bodies == ["v1 body", "v2 body"]


def test_provider_and_source_config_roundtrip(schema_engine: tuple[Engine, str]) -> None:
    """ProviderConfig and SourceConfig round-trip with JSONB params (SPEC §9)."""
    engine, _ = schema_engine
    with Session(engine) as session:
        session.add(
            ProviderConfig(
                provider_type="emulator",
                active_implementation="firebase",
                params={"project": "p1"},
            )
        )
        session.add(
            SourceConfig(
                source_type="apk",
                name="apkmirror",
                resources=[{"url": "https://example/a"}],
                priority=10,
                enabled=True,
            )
        )
        session.commit()

    with Session(engine) as session:
        pc = session.query(ProviderConfig).one()
        assert pc.params == {"project": "p1"}
        sc = session.query(SourceConfig).one()
        assert sc.resources == [{"url": "https://example/a"}]
        assert sc.priority == 10


def test_audit_log_roundtrip(schema_engine: tuple[Engine, str]) -> None:
    """AuditLog records actor/action/object (SPEC §7)."""
    engine, _ = schema_engine
    with Session(engine) as session:
        session.add(
            AuditLog(
                actor="admin",
                action="update_prompt",
                object_ref={"type": "prompt_set", "id": "x"},
            )
        )
        session.commit()

    with Session(engine) as session:
        row = session.query(AuditLog).one()
        assert row.actor == "admin"
        assert row.object_ref["type"] == "prompt_set"
        assert row.created_at.tzinfo is not None
