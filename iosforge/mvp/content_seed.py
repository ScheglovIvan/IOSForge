"""Seed a live Firebase backend with placeholder CONTENT incl. real dummy videos.

Unlike the text-only ``seed_empty.py`` scaffold, this generates a realistic
placeholder catalog (series + episodes), makes tiny ~0.5s MP4 clips + posters
with ffmpeg, uploads them to Firebase Storage (works on the Spark tier), and
writes Firestore documents whose ``videoUrl``/``posterUrl`` point at the uploaded
media. The result is an app you can visually review with *playable* dummy videos;
"swap placeholders → real content" = replace the media / edit docs via Rowy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from iosforge.common.logging import get_logger

log = get_logger("mvp.content_seed")

_COLORS = ["crimson", "royalblue", "seagreen", "orange", "purple", "teal", "hotpink", "gold"]
_GENRES = ["Drama", "Romance", "Revenge", "CEO", "Werewolf", "Fantasy"]


def placeholder_catalog(
    spec: dict[str, Any], *, series_n: int = 4, episodes_n: int = 6
) -> dict[str, list[dict[str, Any]]]:
    """Deterministic placeholder catalog derived from the spec's app type."""
    app_name = str(spec.get("app_name", "App"))
    series = []
    episodes = []
    for s in range(series_n):
        sid = f"series_{s:03d}"
        series.append(
            {
                "id": sid,
                "title": f"{app_name} Placeholder Drama {s + 1}",
                "synopsis": "PLACEHOLDER synopsis — replace with real content.",
                "genres": [_GENRES[s % len(_GENRES)]],
                "episodeCount": episodes_n,
                "freeEpisodeCount": 2,
                "viewCount": 1000 * (s + 1),
            }
        )
        for e in range(episodes_n):
            episodes.append(
                {
                    "id": f"{sid}_ep_{e:03d}",
                    "seriesId": sid,
                    "number": e + 1,
                    "durationSec": 1,
                    "isLocked": e >= 2,
                    "unlockCost": 0 if e < 2 else 20,
                }
            )
    return {"series": series, "episodes": episodes}


def _make_clip(dest: Path, color: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x568:d=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:d=0.5",
            "-pix_fmt",
            "yuv420p",
            "-shortest",
            str(dest),
        ],
        check=True,
        timeout=60,
    )


def _make_poster(dest: Path, color: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x480:d=0.1",
            "-frames:v",
            "1",
            str(dest),
        ],
        check=True,
        timeout=60,
    )


def build_media(out_dir: Path, catalog: dict[str, Any]) -> dict[str, Path]:
    """Generate one poster per series and one 0.5s clip per episode; return id→path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    media: dict[str, Path] = {}
    for i, s in enumerate(catalog["series"]):
        poster = out_dir / f"{s['id']}.png"
        _make_poster(poster, _COLORS[i % len(_COLORS)])
        media[f"poster:{s['id']}"] = poster
    for i, ep in enumerate(catalog["episodes"]):
        clip = out_dir / f"{ep['id']}.mp4"
        _make_clip(clip, _COLORS[i % len(_COLORS)])
        media[f"clip:{ep['id']}"] = clip
    log.info(
        "content_seed.media_built", posters=len(catalog["series"]), clips=len(catalog["episodes"])
    )
    return media


def _bucket_name(project_id: str) -> str:
    return f"{project_id}.firebasestorage.app"


def _ensure_bucket(sa_path: str, project_id: str) -> str:
    """Return the project's Firebase Storage bucket, creating the default one if
    needed. Creating a bucket needs billing (Blaze); on Spark this raises with a
    clear message. Idempotent — reuses an existing bucket."""
    import json as _json
    import urllib.error
    import urllib.request

    import google.auth.transport.requests as gtr
    from google.oauth2 import service_account

    cred = service_account.Credentials.from_service_account_file(
        sa_path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    cred.refresh(gtr.Request())
    tok = cred.token

    def _api(
        url: str, method: str = "GET", body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        data = _json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, _json.loads(r.read() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, _json.loads(e.read() or "{}")

    base = "https://firebasestorage.googleapis.com/v1beta"
    st, body = _api(f"{base}/projects/{project_id}/buckets")
    existing = (
        [b.get("name", "").split("/")[-1] for b in body.get("buckets", [])] if st == 200 else []
    )
    if existing:
        return str(existing[0])

    bucket_id = _bucket_name(project_id)
    st, body = _api(
        f"https://storage.googleapis.com/storage/v1/b?project={project_id}",
        "POST",
        {"name": bucket_id, "location": "US"},
    )
    if st == 403 and "billing" in _json.dumps(body).lower():
        raise RuntimeError(
            "Firebase Storage bucket needs billing (Blaze) — enable Blaze on "
            f"{project_id}, then re-run provisioning."
        )
    _api(f"{base}/projects/{project_id}/buckets/{bucket_id}:addFirebase", "POST", {})
    return bucket_id


def seed_content(
    sa_path: str,
    project_id: str,
    *,
    collection_prefix: str = "",
    work_dir: Path,
    series_n: int = 4,
    episodes_n: int = 6,
    spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Live: build placeholder media, upload to Storage, seed Firestore docs."""
    import firebase_admin
    from firebase_admin import credentials, firestore
    from firebase_admin import storage as fb_storage

    catalog = placeholder_catalog(spec or {}, series_n=series_n, episodes_n=episodes_n)
    media = build_media(work_dir / "media", catalog)
    bucket_name = _ensure_bucket(sa_path, project_id)

    app = firebase_admin.initialize_app(
        credentials.Certificate(sa_path),
        {"projectId": project_id, "storageBucket": bucket_name},
        name=f"content-{project_id}-{work_dir.name}",
    )
    bucket = fb_storage.bucket(app=app)
    db = firestore.client(app)

    def upload(local: Path, remote: str, content_type: str) -> str:
        blob = bucket.blob(f"{collection_prefix}media/{remote}")
        blob.upload_from_filename(str(local), content_type=content_type)
        blob.make_public()
        return str(blob.public_url)

    series_coll = f"{collection_prefix}Series"
    episode_coll = f"{collection_prefix}Episode"
    for s in catalog["series"]:
        s["posterUrl"] = upload(media[f"poster:{s['id']}"], f"{s['id']}.png", "image/png")
        db.collection(series_coll).document(s["id"]).set(s)
    for ep in catalog["episodes"]:
        ep["videoUrl"] = upload(media[f"clip:{ep['id']}"], f"{ep['id']}.mp4", "video/mp4")
        db.collection(episode_coll).document(ep["id"]).set(ep)

    counts = {"series": len(catalog["series"]), "episodes": len(catalog["episodes"])}
    log.info("content_seed.done", project=project_id, **counts)
    return {"bucket": bucket_name, "counts": counts}
