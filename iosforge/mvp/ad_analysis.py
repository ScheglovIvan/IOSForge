"""On-demand ad analysis: infer ad networks + placements from marked ad frames.

Opt-in step (Settings.analyze_ads). Reads the ad frames that
:mod:`iosforge.mvp.screen_filter` preserved into ``ad_screens/`` (with their
sequence/trigger context in ``screen_labels.json``), asks the local Claude CLI
(vision) to produce a structured ad model, writes ``ad_analysis.json`` and merges
``ad_placements`` / ``ad_networks`` back into ``app_spec.json.monetization``.

Honest limit: format + placement are reliably inferable from a recording, but the
ad NETWORK is best-effort (from on-screen creative only) — the exact mediation SDK
(AppLovin MAX / ironSource / AdMob) needs APK static analysis, which is not done.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from iosforge.common.logging import get_logger
from iosforge.mvp import spec_contract
from iosforge.mvp.paths import RunPaths

log = get_logger("mvp.ad_analysis")

CLAUDE_BIN = "claude"
AD_TOOLS = "Read Write Glob"
_ALLOWED_FORMATS = frozenset({"banner", "interstitial", "rewarded", "native", "offerwall"})

AD_ANALYSIS_PROMPT = """\
The `ad_screens/` directory holds PNG frames captured from a phone SCREEN RECORDING
that a prior step classified as ADVERTISEMENTS. `ad_frames.json` lists each ad
frame with its `ordinal` (position in the recording) and the nearest app screens
before/after it (`prev_app_id` / `next_app_id`) — use these to infer WHEN/WHERE the
ad fires. `monetization.json` (if present) is the app's monetization context.

LOOK at every ad frame (vision) and produce ONLY a file `ad_analysis.json`:

{
  "ad_networks": [
    {"name": str,               // e.g. AdMob, AppLovin, Unity Ads, ironSource
     "confidence": "high|medium|low",
     "evidence": str,           // on-screen creative / store-redirect card / branding you saw
     "source": "video_creative"}
  ],
  "ad_placements": [
    {"format": "banner|interstitial|rewarded|native|offerwall",
     "trigger": str,            // "after closing a series", "reward button tap", "on home load"
     "screen_context": str,     // which app flow it interrupts (use prev/next app ids)
     "frequency": str,          // "every N screens" | "once per session" | "on demand"
     "frames": [str]}           // source ad-frame filenames
  ],
  "notes": {
    "network_detection": "best-effort from on-screen creative only; exact mediation SDK "
                         "(AppLovin MAX / ironSource / AdMob) needs APK static analysis.",
    "assumptions": [str]
  }
}

Rules:
- `format` and `trigger`/`screen_context` are REQUIRED per placement and reliably inferable.
- `ad_networks` is best-effort: only list a network when the creative/store chrome names or
  clearly brands it; ALWAYS set `confidence` + `evidence`; never assert the mediation SDK.
- Group frames that are the same ad occurrence into one placement.
Output nothing but `ad_analysis.json`.
"""


def _run_claude(prompt: str, workdir: Path, timeout: int) -> None:
    cmd = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--permission-mode",
        "acceptEdits",
        "--allowed-tools",
        AD_TOOLS,
    ]
    res = subprocess.run(
        cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout, check=False
    )
    if res.returncode != 0:
        log.error("ad_analysis.cli_failed", code=res.returncode, stderr=res.stderr[-2000:])
        raise RuntimeError(f"claude CLI exited {res.returncode}: {res.stderr[-500:]}")


def _normalise(payload: object) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    networks = data.get("ad_networks", [])
    placements = data.get("ad_placements", [])
    clean_placements = [
        p
        for p in (placements if isinstance(placements, list) else [])
        if isinstance(p, dict) and p.get("format") in _ALLOWED_FORMATS
    ]
    return {
        "ad_networks": networks if isinstance(networks, list) else [],
        "ad_placements": clean_placements,
        "notes": data.get("notes", {}) if isinstance(data.get("notes"), dict) else {},
    }


def _merge_into_spec(paths: RunPaths, model: dict[str, Any]) -> None:
    if not paths.app_spec_json.is_file():
        return
    spec = json.loads(paths.app_spec_json.read_text())
    if not isinstance(spec, dict):
        return
    monetization = spec.get("monetization")
    if not isinstance(monetization, dict):
        monetization = {}
    monetization["ad_placements"] = model["ad_placements"]
    monetization["ad_networks"] = model["ad_networks"]
    spec["monetization"] = monetization
    try:
        validated = spec_contract.validate_spec(spec)
    except spec_contract.SpecValidationError as exc:
        log.warning("ad_analysis.merge_rejected", error=str(exc)[:300])
        return
    paths.app_spec_json.write_text(json.dumps(validated, indent=2, ensure_ascii=False))


def analyze_ads(paths: RunPaths, *, timeout: int = 900) -> Path | None:
    """Analyse preserved ad frames into a structured model; return its path or None."""
    if not paths.screen_labels_json.is_file():
        log.info("ad_analysis.no_labels")
        return None
    audit = json.loads(paths.screen_labels_json.read_text())
    ads = audit.get("ads", []) if isinstance(audit, dict) else []
    if not ads or not paths.ad_screens_dir.is_dir():
        log.info("ad_analysis.no_ads", ads=len(ads))
        return None

    ws = paths.run_dir / "ad_ws"
    ws_ad = ws / "ad_screens"
    if ws_ad.exists():
        shutil.rmtree(ws_ad)
    shutil.copytree(paths.ad_screens_dir, ws_ad)
    (ws / "ad_frames.json").write_text(json.dumps({"ads": ads}, indent=2, ensure_ascii=False))
    if paths.app_spec_json.is_file():
        spec = json.loads(paths.app_spec_json.read_text())
        if isinstance(spec, dict) and isinstance(spec.get("monetization"), dict):
            (ws / "monetization.json").write_text(
                json.dumps(spec["monetization"], indent=2, ensure_ascii=False)
            )
    (ws / "AD_ANALYSIS_PROMPT.md").write_text(AD_ANALYSIS_PROMPT)

    _run_claude(AD_ANALYSIS_PROMPT, ws, timeout)

    produced = ws / "ad_analysis.json"
    if not produced.exists():
        raise RuntimeError("Claude did not produce ad_analysis.json")
    model = _normalise(json.loads(produced.read_text()))
    paths.ad_analysis_json.write_text(json.dumps(model, indent=2, ensure_ascii=False))
    _merge_into_spec(paths, model)

    log.info(
        "ad_analysis.done",
        networks=len(model["ad_networks"]),
        placements=len(model["ad_placements"]),
    )
    return paths.ad_analysis_json
