"""Artifact storage tests (T-2.3, SPEC §8, §10).

Covers the :class:`ArtifactStorage` abstraction over the versioned MinIO/S3
bucket from ``get_settings()``:

* ``put`` -> ``get`` round-trips the exact bytes;
* a second ``put`` of the same key produces a *new* ``version_id`` while both
  versions remain individually retrievable (bucket versioning, SPEC §10);
* artifact keys are bound to a Job via the ``jobs/{job_id}/{kind}/{name}``
  layout, and the returned :class:`ArtifactRef` carries that ``job_id``/``kind``;
* ``exists`` and ``list_versions`` reflect what was written.

Isolation: every test uses a unique ``job_id`` so keys never collide and the
suite is safe to run alongside parallel agents. Objects written by a test are
removed in teardown.

Skip policy (mirrors ``test_db``): if MinIO is unreachable or the configured
credentials cannot access the bucket (e.g. no ``.env`` in this environment), the
whole module is skipped instead of failing — the run that owns this task
provides a live, authenticated MinIO.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator

import pytest

from iosforge.common.config import get_settings
from iosforge.storage import ArtifactRef, ArtifactStorage, S3ArtifactStorage, build_key


def _storage_reachable() -> bool:
    try:
        storage = S3ArtifactStorage()
        return storage.bucket_ready()
    except Exception:  # pragma: no cover - infra/creds missing
        return False


pytestmark = pytest.mark.skipif(
    not _storage_reachable(),
    reason="requires live, authenticated MinIO bucket (see docs/local-infra.md)",
)


@pytest.fixture()
def storage() -> S3ArtifactStorage:
    return S3ArtifactStorage()


@pytest.fixture()
def job_id() -> str:
    return f"t2-3-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def cleanup(storage: S3ArtifactStorage) -> Iterator[list[str]]:
    """Collect keys written during a test and purge all their versions after."""
    written: list[str] = []
    try:
        yield written
    finally:
        for key in written:
            storage.purge(key)


def test_build_key_binds_artifact_to_job() -> None:
    """Keys follow the ``jobs/{job_id}/{kind}/{name}`` Job-scoped layout."""
    key = build_key(job_id="job-42", kind="screenshots", name="screen_01.png")
    assert key == "jobs/job-42/screenshots/screen_01.png"


def test_build_key_rejects_path_traversal() -> None:
    """Components must not escape the Job prefix."""
    with pytest.raises(ValueError):
        build_key(job_id="job-42", kind="apk", name="../escape.apk")


def test_put_then_get_roundtrips_bytes(
    storage: S3ArtifactStorage, job_id: str, cleanup: list[str]
) -> None:
    """``get`` returns exactly the bytes that ``put`` stored."""
    key = build_key(job_id=job_id, kind="report", name="result.json")
    cleanup.append(key)
    payload = b'{"compliance": 0.97}'

    ref = storage.put(key, payload, content_type="application/json")

    assert isinstance(ref, ArtifactRef)
    assert ref.bucket == get_settings().s3_bucket
    assert ref.key == key
    assert ref.job_id == job_id
    assert ref.kind == "report"
    assert ref.content_type == "application/json"
    assert ref.size == len(payload)
    assert storage.get(key) == payload


def test_put_accepts_file_object(
    storage: S3ArtifactStorage, job_id: str, cleanup: list[str]
) -> None:
    """A binary file-like object is uploaded the same as raw bytes."""
    key = build_key(job_id=job_id, kind="sources", name="src.zip")
    cleanup.append(key)
    payload = b"PK\x03\x04 zip-bytes"

    ref = storage.put(key, io.BytesIO(payload))

    assert ref.size == len(payload)
    assert storage.get(key) == payload


def test_second_put_creates_new_version_both_retrievable(
    storage: S3ArtifactStorage, job_id: str, cleanup: list[str]
) -> None:
    """Re-putting the same key yields a new version; both stay retrievable."""
    key = build_key(job_id=job_id, kind="report", name="versioned.txt")
    cleanup.append(key)

    ref_v1 = storage.put(key, b"first")
    ref_v2 = storage.put(key, b"second")

    assert ref_v1.version_id is not None
    assert ref_v2.version_id is not None
    assert ref_v1.version_id != ref_v2.version_id

    # Latest read returns the most recent write...
    assert storage.get(key) == b"second"
    # ...and each historical version is individually addressable.
    assert storage.get(key, version_id=ref_v1.version_id) == b"first"
    assert storage.get(key, version_id=ref_v2.version_id) == b"second"


def test_list_versions_returns_all_writes_newest_first(
    storage: S3ArtifactStorage, job_id: str, cleanup: list[str]
) -> None:
    """``list_versions`` reports every stored version of a key."""
    key = build_key(job_id=job_id, kind="report", name="history.txt")
    cleanup.append(key)

    ref_v1 = storage.put(key, b"one")
    ref_v2 = storage.put(key, b"two")

    versions = storage.list_versions(key)
    version_ids = [v.version_id for v in versions]
    assert ref_v1.version_id in version_ids
    assert ref_v2.version_id in version_ids
    assert versions[0].is_latest is True
    assert versions[0].version_id == ref_v2.version_id


def test_exists_reflects_presence(
    storage: S3ArtifactStorage, job_id: str, cleanup: list[str]
) -> None:
    """``exists`` is False before a put and True after."""
    key = build_key(job_id=job_id, kind="apk", name="app.apk")
    cleanup.append(key)

    assert storage.exists(key) is False
    storage.put(key, b"dex")
    assert storage.exists(key) is True


def test_soft_delete_keeps_history_on_versioned_bucket(
    storage: S3ArtifactStorage, job_id: str, cleanup: list[str]
) -> None:
    """A soft ``delete`` hides the current object but preserves prior versions."""
    key = build_key(job_id=job_id, kind="report", name="deletable.txt")
    cleanup.append(key)

    ref = storage.put(key, b"keep-me")
    storage.delete(key)

    # The latest read no longer resolves (delete marker on a versioned bucket)...
    assert storage.exists(key) is False
    # ...but the concrete prior version is still retrievable.
    assert storage.get(key, version_id=ref.version_id) == b"keep-me"


def test_storage_satisfies_protocol(storage: S3ArtifactStorage) -> None:
    """The S3 implementation is usable through the abstract :class:`ArtifactStorage`."""
    assert isinstance(storage, ArtifactStorage)
