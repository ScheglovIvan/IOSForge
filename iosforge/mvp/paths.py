"""Single source of truth for the run-directory layout (keeps steps in sync)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunPaths:
    """Layout of one MVP run: runs/<ts>/{screens/,screens.json,claude_ws/,flutter_app/}.

    ``app_spec_json`` and ``tasks_json`` are the canonical outputs of the
    analysis (Stage B) and decomposition (Stage C) steps that sit between the
    crawl and codegen passes.

    ``apk``, ``generated_screens_dir``, ``generated_screens_json``,
    ``selftest_report_json`` and ``corrective_tasks_json`` are the Stage E
    (compliance/refinement) artifacts: the built Flutter APK, the deep-link
    render of each generated screen, the compliance report and the corrective
    task list.
    """

    run_dir: Path
    screens_dir: Path
    screens_json: Path
    source_dir: Path
    fonts_json: Path
    fonts_dir: Path
    media_dir: Path
    network_jsonl: Path
    json_bodies_jsonl: Path
    media_json: Path
    subscriptions_json: Path
    sdks_json: Path
    ads_raw_json: Path
    network_index_json: Path
    ad_screens_dir: Path
    ad_analysis_json: Path
    screen_labels_json: Path
    claude_ws: Path
    flutter_app: Path
    app_spec_json: Path
    spec_md: Path
    handoff_dir: Path
    tasks_json: Path
    rc_config_json: Path
    apk: Path
    generated_screens_dir: Path
    generated_screens_json: Path
    selftest_report_json: Path
    corrective_tasks_json: Path

    @classmethod
    def create(cls, base: Path) -> RunPaths:
        ts = time.strftime("%Y%m%d-%H%M%S")
        run_dir = base / ts
        rp = cls(
            run_dir=run_dir,
            screens_dir=run_dir / "screens",
            screens_json=run_dir / "screens.json",
            source_dir=run_dir / "source",
            fonts_json=run_dir / "fonts.json",
            fonts_dir=run_dir / "fonts",
            media_dir=run_dir / "media",
            network_jsonl=run_dir / "network.jsonl",
            json_bodies_jsonl=run_dir / "json_bodies.jsonl",
            media_json=run_dir / "media.json",
            subscriptions_json=run_dir / "subscriptions.json",
            sdks_json=run_dir / "sdks.json",
            ads_raw_json=run_dir / "ads_raw.json",
            network_index_json=run_dir / "network_index.json",
            ad_screens_dir=run_dir / "ad_screens",
            ad_analysis_json=run_dir / "ad_analysis.json",
            screen_labels_json=run_dir / "screen_labels.json",
            claude_ws=run_dir / "claude_ws",
            flutter_app=run_dir / "flutter_app",
            app_spec_json=run_dir / "app_spec.json",
            spec_md=run_dir / "SPEC.md",
            handoff_dir=run_dir / "handoff",
            tasks_json=run_dir / "tasks.json",
            rc_config_json=run_dir / "rc_config.json",
            apk=run_dir / "generated.apk",
            generated_screens_dir=run_dir / "generated_screens",
            generated_screens_json=run_dir / "generated_screens.json",
            selftest_report_json=run_dir / "selftest_report.json",
            corrective_tasks_json=run_dir / "corrective_tasks.json",
        )
        rp.screens_dir.mkdir(parents=True, exist_ok=True)
        rp.generated_screens_dir.mkdir(parents=True, exist_ok=True)
        return rp
