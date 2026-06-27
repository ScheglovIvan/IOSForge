"""MVP entrypoint: APK -> emulator crawl -> screenshots+map -> Claude Code -> Flutter app.

Usage:
    uv run python -m iosforge.mvp.cli run <app.apk> [--out runs] [--max-screens N] [--avd mvp]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from iosforge.common.config import get_settings
from iosforge.common.logging import configure_logging, get_logger
from iosforge.mvp import claude_gen, compliance, crawl, emulator
from iosforge.mvp.analyze import analyze, decompose
from iosforge.mvp.paths import RunPaths

log = get_logger("mvp.cli")


def run(apk: Path, out_base: Path, max_screens: int, avd: str, do_build_check: bool) -> Path:
    if not apk.exists():
        raise SystemExit(f"APK not found: {apk}")

    paths = RunPaths.create(out_base)
    log.info("mvp.run.start", apk=str(apk), run_dir=str(paths.run_dir), max_screens=max_screens)

    emulator.start_emulator(avd)
    emulator.wait_for_boot()
    package = emulator.install_apk(apk)
    emulator.launch(package)

    crawl.walk(paths, package, max_screens)

    analyze(paths)
    decompose(paths)
    flutter_app = claude_gen.generate_from_tasks(paths)

    settings = get_settings()
    report = compliance.refine_until_compliant(
        paths,
        threshold=settings.compliance_threshold,
        soft_floor=settings.compliance_soft_floor,
        max_iterations=settings.compliance_max_iterations,
        weights=compliance.ComplianceWeights.from_settings(settings),
        avd=avd,
    )
    if do_build_check:
        claude_gen.build_check(flutter_app)

    log.info(
        "mvp.run.done",
        flutter_app=str(flutter_app),
        compliance_score=report.get("compliance_score"),
        status=report.get("status"),
    )
    print(f"\nDONE. Flutter app: {flutter_app}")
    print(f"Compliance:      {report.get('compliance_score')} ({report.get('status')})")
    print(f"Screens + map:   {paths.screens_json}")
    return flutter_app


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="iosforge-mvp")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run the full vertical on a local APK")
    p.add_argument("apk", type=Path, help="path to the local .apk file")
    p.add_argument("--out", type=Path, default=Path("runs"), help="base output dir")
    p.add_argument("--max-screens", type=int, default=get_settings().walkthrough_max_screens)
    p.add_argument("--avd", default="mvp", help="AVD name to boot")
    p.add_argument("--no-build-check", action="store_true", help="skip flutter analyze")

    args = parser.parse_args(argv)
    if args.cmd == "run":
        run(args.apk, args.out, args.max_screens, args.avd, not args.no_build_check)
    return 0


if __name__ == "__main__":
    sys.exit(main())
