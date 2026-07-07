"""Data-archive upload validation tests (untrusted file handling)."""

from __future__ import annotations

import gzip
import io
import tarfile
import zipfile

import pytest

from iosforge.admin.archive_validate import (
    ArchiveValidationError,
    sanitize_filename,
    validate,
)


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data/frida.json", b"{}")
    return buf.getvalue()


def _targz_bytes() -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        info = tarfile.TarInfo("data/frida.json")
        payload = b"{}"
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return gzip.compress(raw.getvalue())


def _tar_bytes() -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        info = tarfile.TarInfo("data/frida.json")
        payload = b"{}"
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return raw.getvalue()


def test_accepts_zip() -> None:
    v = validate("archive.zip", _zip_bytes(), 10_000_000)
    assert v.container == "zip"
    assert v.filename == "archive.zip"
    assert len(v.sha256) == 64


def test_accepts_tar_gz_and_tgz() -> None:
    data = _targz_bytes()
    assert validate("dump.tar.gz", data, 10_000_000).container == "tar.gz"
    assert validate("dump.tgz", data, 10_000_000).container == "tar.gz"


def test_accepts_plain_tar() -> None:
    assert validate("dump.tar", _tar_bytes(), 10_000_000).container == "tar"


def test_rejects_unknown_extension() -> None:
    with pytest.raises(ArchiveValidationError):
        validate("data.mp4", _zip_bytes(), 10_000_000)


def test_rejects_empty() -> None:
    with pytest.raises(ArchiveValidationError):
        validate("archive.zip", b"", 10_000_000)


def test_rejects_oversize() -> None:
    with pytest.raises(ArchiveValidationError):
        validate("archive.zip", _zip_bytes(), 4)


def test_rejects_wrong_magic() -> None:
    with pytest.raises(ArchiveValidationError):
        validate("archive.zip", b"not a zip at all", 10_000_000)
    with pytest.raises(ArchiveValidationError):
        validate("dump.tar.gz", b"not gzip", 10_000_000)


def test_tar_gz_wins_over_tar_suffix() -> None:
    assert validate("dump.tar.gz", _targz_bytes(), 10_000_000).container == "tar.gz"


def test_sanitize_strips_paths_and_forces_extension() -> None:
    assert sanitize_filename("../../etc/passwd") == "passwd.zip"
    assert sanitize_filename("a b/c.zip") == "c.zip"
