"""Artifact storage abstraction (MinIO/S3 client) + versioning.

Public surface (SPEC §8, §10; contract documented by T-2.4 in API_MAP):

* :func:`build_key` — Job-scoped object key layout.
* :class:`ArtifactRef` / :class:`ArtifactVersion` — stored-object descriptors.
* :class:`ArtifactStorage` — backend-agnostic ABC (swappable per SPEC §9).
* :class:`S3ArtifactStorage` — boto3/MinIO implementation from ``get_settings()``.
"""

from iosforge.storage.client import (
    ArtifactRef,
    ArtifactStorage,
    ArtifactVersion,
    S3ArtifactStorage,
    build_key,
)

__all__ = [
    "ArtifactRef",
    "ArtifactStorage",
    "ArtifactVersion",
    "S3ArtifactStorage",
    "build_key",
]
