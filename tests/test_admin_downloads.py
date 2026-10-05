"""Downloading the finished assets: the exact files App Store Connect wants."""

from __future__ import annotations

import io
import struct
import zipfile

from iosforge.admin.routes_jobs import _SAFE_NAME


def test_download_name_is_recognisable_and_safe() -> None:
    # the file lands in the operator's Downloads folder, so it carries the app name
    assert _SAFE_NAME.sub("-", "Dashboard for CarPlay").strip("-") == "Dashboard-for-CarPlay"
    assert _SAFE_NAME.sub("-", "Speaker Cleaner") == "Speaker-Cleaner"


def test_download_name_strips_path_and_header_breaking_characters() -> None:
    # the name goes straight into a Content-Disposition header and must not be able
    # to escape it or to walk a path
    for hostile in ('a"b', "a/b", "a\\b", "a\r\nb", "../../etc/passwd"):
        cleaned = _SAFE_NAME.sub("-", hostile)
        assert '"' not in cleaned
        assert "/" not in cleaned and "\\" not in cleaned
        assert "\r" not in cleaned and "\n" not in cleaned


def test_zip_keeps_the_stored_bytes() -> None:
    # a listing upload needs the exact 1290x2796 PNGs, never a rescale
    payloads = {"01.png": b"FIRST-PNG", "02.png": b"SECOND-PNG"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, data in payloads.items():
            archive.writestr(name, data)

    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        assert sorted(archive.namelist()) == ["01.png", "02.png"]
        for name, data in payloads.items():
            assert archive.read(name) == data


def test_slide_listing_survives_a_gap(monkeypatch) -> None:
    # slides can be deleted individually; a listing that starts at 02 is still a
    # listing. Stopping at the first gap hid the whole set once 01 was removed.
    import uuid as _uuid

    from iosforge.admin import routes_jobs

    present = {"02", "03", "05"}

    class _Storage:
        def exists(self, key: str) -> bool:
            return key.rsplit("/", 1)[-1][:2] in present

    monkeypatch.setattr(routes_jobs, "S3ArtifactStorage", _Storage, raising=False)
    monkeypatch.setitem(
        __import__("sys").modules["iosforge.storage.client"].__dict__,
        "S3ArtifactStorage",
        _Storage,
    )
    keys = routes_jobs._store_slides(_uuid.uuid4())
    assert [k.rsplit("/", 1)[-1] for k in keys] == ["02.png", "03.png", "05.png"]


def test_delete_answers_fetch_without_a_redirect() -> None:
    # the admin removes the tile itself; a redirect would make the browser reload a
    # whole page worth of galleries for a one-tile change
    import uuid as _uuid

    from starlette.requests import Request as _Request

    from iosforge.admin.routes_jobs import _deleted

    def _req(headers: list[tuple[bytes, bytes]]) -> _Request:
        return _Request({"type": "http", "method": "POST", "path": "/", "headers": headers})

    job = _uuid.uuid4()
    fetch = _req([(b"x-requested-with", b"fetch")])
    assert _deleted(fetch, job).status_code == 204
    assert _deleted(fetch, job, ok=False).status_code == 409
    # a browser without scripting still gets the page back
    assert _deleted(_req([]), job).status_code == 303


def test_slide_name_accepts_phone_and_tablet_references() -> None:
    from iosforge.admin.routes_jobs import _slide_name

    assert _slide_name("3") == "03.png"
    assert _slide_name("ipad_02") == "ipad_02.png"
    assert _slide_name(" IPAD_7 ") == "ipad_07.png"


def test_slide_name_rejects_anything_else() -> None:
    # the value goes into a storage key, so an arbitrary string must not pass
    from iosforge.admin.routes_jobs import _slide_name

    for hostile in ("", "0", "11", "../secret", "ipad_", "ipad_x", "1;rm", "slide_contracts"):
        assert _slide_name(hostile) is None


def test_download_names_distinguish_the_two_sets() -> None:
    # both archives land in the same Downloads folder, so they must not collide
    from iosforge.admin.routes_jobs import _SAFE_NAME

    app = _SAFE_NAME.sub("-", "Dashboard for CarPlay").strip("-")
    assert f"{app}-screenshots.zip" != f"{app}-ipad-screenshots.zip"


def test_slot_sizes_match_what_app_store_connect_demands() -> None:
    # observed live: the 6.5" slot refused a 1290x2796 set, naming exactly these
    from iosforge.admin.routes_jobs import DEVICE_SIZES

    assert DEVICE_SIZES["iphone-69"] == (1290, 2796)
    assert DEVICE_SIZES["iphone-65"] == (1284, 2778)
    assert DEVICE_SIZES["ipad-129"] == (2048, 2732)


def test_neighbouring_iphone_slots_are_the_same_shape() -> None:
    # which is why rescaling between them is visually lossless rather than a redesign
    from iosforge.admin.routes_jobs import DEVICE_SIZES

    a = DEVICE_SIZES["iphone-69"]
    b = DEVICE_SIZES["iphone-65"]
    assert abs(a[0] / a[1] - b[0] / b[1]) < 0.002


def _image(codec: str, size: str) -> bytes:
    import subprocess

    args = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=red:s={size}", "-frames:v", "1"]
    args += ["-f", "mjpeg", "-"] if codec == "jpeg" else ["-f", "image2", "-c:v", "png", "-"]
    return subprocess.run(args, capture_output=True, check=False).stdout


def test_native_download_transcodes_a_jpeg_the_model_mislabelled() -> None:
    # App Store Connect checks the contents against the extension: JPEG bytes under a
    # .png name displayed as a broken slide in the 6.9" slot
    from iosforge.admin.routes_jobs import slide_for_slot

    out = slide_for_slot(_image("jpeg", "1290x2796"), None)
    assert out[:8] == b"\x89PNG\r\n\x1a\n"


def test_native_download_leaves_a_real_png_byte_for_byte() -> None:
    from iosforge.admin.routes_jobs import slide_for_slot

    png = _image("png", "64x64")
    assert slide_for_slot(png, None) == png


def test_resized_download_is_a_png_at_the_slot_size() -> None:
    from iosforge.admin.routes_jobs import DEVICE_SIZES, slide_for_slot

    out = slide_for_slot(_image("jpeg", "1290x2796"), DEVICE_SIZES["iphone-65"])
    assert out[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", out[16:24]) == DEVICE_SIZES["iphone-65"]
