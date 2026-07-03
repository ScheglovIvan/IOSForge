"""ORM models for the IOSForge pipeline (T-2.2, SPEC §10).

Design notes
------------
* **UUID primary keys** everywhere (SPEC §10), generated client-side so artefacts
  can be referenced before a flush.
* **Timezone-aware timestamps** (``timestamptz``) for every ``created_at`` /
  ``updated_at`` / event time.
* **JSONB** for the flexible / schemaless fields called out in §10 (source-app
  metadata, screen map, provider params, source resource lists, audit object refs).
* ``Job.state`` reuses the canonical :class:`~iosforge.common.types.JobState`
  enum (SPEC §5.1) rather than redefining a parallel set of literals.

**Timeline modelling choice.** SPEC §10/§5.1 require a per-Job timeline (when each
stage started/finished, durations, errors). We model it as a dedicated
:class:`StageTimeline` table (one row per Job × Stage attempt) instead of a JSONB
blob on ``Job``. Rationale: the orchestrator queries/aggregates stage durations and
errors for the monitoring dashboard (SPEC §8), and idempotency keys are per
``(job_id, stage, input_hash)`` (STACK) — both are far cleaner against real rows
than against a JSON array. The stage enum reuses
:class:`~iosforge.common.types.Stage`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from iosforge.common.types import JobState, Stage
from iosforge.db.base import Base, utcnow, uuid_pk


def _ts_col(*, onupdate: bool = False) -> Mapped[dt.datetime]:
    """A non-null ``timestamptz`` column defaulting to UTC now."""
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow if onupdate else None,
    )


class Job(Base):
    """A single end-to-end cloning task (SPEC §10 Job, §5.1 lifecycle)."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = uuid_pk()

    # Source iOS app: reference (link/ID) + free-form extracted metadata (JSONB).
    source_app_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    source_app_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    # Lifecycle state — reuses the canonical JobState enum (SPEC §5.1).
    state: Mapped[JobState] = mapped_column(
        Enum(JobState, name="job_state", native_enum=True),
        nullable=False,
        default=JobState.QUEUED,
    )

    # Selected providers / prompt set / versions (SPEC §10).
    emulator_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    catalog_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    apk_source_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    codegen_target: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prompt_versions.id", ondelete="SET NULL"), nullable=True
    )
    result_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Manual-upload admin path (SPEC §7): who submitted + how. Nullable so the
    # discovery/acquisition path (EPIC-1) is unaffected.
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    submission_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="manual_apk")

    created_at: Mapped[dt.datetime] = _ts_col()
    updated_at: Mapped[dt.datetime] = _ts_col(onupdate=True)

    # Relationships (children cascade-delete with the Job).
    candidates: Mapped[list[Candidate]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    apk_artifacts: Mapped[list[ApkArtifact]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    video_artifacts: Mapped[list[VideoArtifact]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    walkthrough_results: Mapped[list[WalkthroughResult]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    generation_results: Mapped[list[GenerationResult]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    timeline: Mapped[list[StageTimeline]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    prompt_version: Mapped[PromptVersion | None] = relationship()


class AdminUser(Base):
    """An operator account for the admin panel (SPEC §7 auth).

    No public registration: rows are created by the seed command. Password is
    stored only as an argon2 hash; the plaintext never touches the DB or logs.
    """

    __tablename__ = "admin_users"
    __table_args__ = (UniqueConstraint("username", name="admin_user_username"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[dt.datetime] = _ts_col()
    updated_at: Mapped[dt.datetime] = _ts_col(onupdate=True)


class StageTimeline(Base):
    """One stage attempt of a Job: timing, duration, error (SPEC §5.1, §8)."""

    __tablename__ = "stage_timeline"
    __table_args__ = (UniqueConstraint("job_id", "stage", "input_hash", name="stage_idempotency"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage: Mapped[Stage] = mapped_column(
        Enum(Stage, name="pipeline_stage", native_enum=True), nullable=False
    )
    # Natural idempotency key component (STACK): hash of the stage inputs.
    input_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = _ts_col()

    job: Mapped[Job] = relationship(back_populates="timeline")


class Candidate(Base):
    """A discovered Android analogue of the source app (SPEC §5.2, §10)."""

    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    package_id: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    publisher: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Match confidence (name/publisher/icon/category) in [0, 1].
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[dt.datetime] = _ts_col()

    job: Mapped[Job] = relationship(back_populates="candidates")


class ApkArtifact(Base):
    """A downloaded APK + its manifest (SPEC §5.3, §10)."""

    __tablename__ = "apk_artifacts"

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Storage key/path in the artefact store (SPEC §8 MinIO/S3).
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[dt.datetime] = _ts_col()

    job: Mapped[Job] = relationship(back_populates="apk_artifacts")


class VideoArtifact(Base):
    """An uploaded screen-recording used as the walkthrough source (SPEC §5.4)."""

    __tablename__ = "video_artifacts"

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    container: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[dt.datetime] = _ts_col()

    job: Mapped[Job] = relationship(back_populates="video_artifacts")


class WalkthroughResult(Base):
    """Emulator walkthrough output: screenshots, screen map, logs (SPEC §5.4, §10)."""

    __tablename__ = "walkthrough_results"

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Storage keys for the per-screen screenshots.
    screenshot_keys: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    # Screen map (navigation graph) — schemaless JSONB.
    screen_map: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    logs: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    strategy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[dt.datetime] = _ts_col()

    job: Mapped[Job] = relationship(back_populates="walkthrough_results")


class GenerationResult(Base):
    """Claude Code generation output + compliance report (SPEC §5.5, §10)."""

    __tablename__ = "generation_results"

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Storage key/path to the generated Flutter sources.
    sources_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Compliance score in [0, 1] (the ">= 95%" metric, SPEC §5.5).
    compliance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    selftest_report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prompt_versions.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[dt.datetime] = _ts_col()

    job: Mapped[Job] = relationship(back_populates="generation_results")
    prompt_version: Mapped[PromptVersion | None] = relationship()


class PromptSet(Base):
    """A named folder/purpose of Claude Code prompts (SPEC §6, §10)."""

    __tablename__ = "prompt_sets"
    __table_args__ = (UniqueConstraint("name", name="prompt_set_name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # Purpose folder: analysis / admin-content / business-logic / generation / selftest.
    purpose: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = _ts_col()
    updated_at: Mapped[dt.datetime] = _ts_col(onupdate=True)

    versions: Mapped[list[PromptVersion]] = relationship(
        back_populates="prompt_set", cascade="all, delete-orphan"
    )


class PromptVersion(Base):
    """An immutable version of a prompt set's body (SPEC §6, §10)."""

    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("prompt_set_id", "version", name="prompt_version_seq"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    prompt_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("prompt_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(String(256), nullable=True)

    created_at: Mapped[dt.datetime] = _ts_col()

    prompt_set: Mapped[PromptSet] = relationship(back_populates="versions")


class ProviderConfig(Base):
    """Active implementation + params for a provider abstraction (SPEC §9, §10)."""

    __tablename__ = "provider_configs"
    __table_args__ = (UniqueConstraint("provider_type", name="provider_type_unique"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    # e.g. emulator / catalog / apk_source / codegen / build.
    provider_type: Mapped[str] = mapped_column(String(128), nullable=False)
    active_implementation: Mapped[str] = mapped_column(String(128), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[dt.datetime] = _ts_col()
    updated_at: Mapped[dt.datetime] = _ts_col(onupdate=True)


class SourceConfig(Base):
    """A configurable catalog/APK source with priority + toggle (SPEC §5.2/§5.3, §10)."""

    __tablename__ = "source_configs"

    id: Mapped[uuid.UUID] = uuid_pk()
    # 'catalog' (Android app catalog) or 'apk' (APK resource).
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    resources: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[dt.datetime] = _ts_col()
    updated_at: Mapped[dt.datetime] = _ts_col(onupdate=True)


class AuditLog(Base):
    """Who changed what, when (SPEC §7 audit, §10)."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = uuid_pk()
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    action: Mapped[str] = mapped_column(String(256), nullable=False)
    # Target object reference (type + id + diff), schemaless.
    object_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[dt.datetime] = _ts_col()


__all__ = [
    "AdminUser",
    "ApkArtifact",
    "AuditLog",
    "Candidate",
    "GenerationResult",
    "Job",
    "PromptSet",
    "PromptVersion",
    "ProviderConfig",
    "SourceConfig",
    "StageTimeline",
    "WalkthroughResult",
]
