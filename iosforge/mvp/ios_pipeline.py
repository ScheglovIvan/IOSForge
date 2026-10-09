"""End-to-end SwiftUI run on a Mac worker: codegen → Vision Judge on the iOS Simulator.

``python -m iosforge.mvp.ios_pipeline`` generates the app from a real job's inputs (or
reuses an existing run with ``--run-dir``), then runs
:func:`iosforge.mvp.compliance.refine_ios_until_complete` with the configured
thresholds and iteration cap. Exits 2 without the Apple toolchain (no silent pass).
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from iosforge.common.config import get_settings
from iosforge.mvp import compliance, frida_ingest, simulator, swiftui_gen, xcode
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.source_locale import resolve_source_locale


def _generate(args: argparse.Namespace) -> RunPaths:
    paths = RunPaths.create(args.out)
    shutil.copy2(args.app_spec, paths.app_spec_json)
    frida_ingest.ingest_archive(args.archive, paths)
    if not args.locale:
        args.locale = _resolve_locale(paths, args.storefront)
    spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
    wanted = args.screens or [str(s["id"]) for s in spec.get("screens", [])]
    swiftui_gen.scope_to(paths, [sid for sid in wanted if sid not in set(args.exclude)])
    spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
    app_name = args.app_name or str(spec.get("app_name") or "Generated App")
    result = swiftui_gen.generate(
        paths,
        app_name=app_name,
        bundle_id=args.bundle_id,
        max_parallel=args.max_parallel,
        settings=get_settings(),
    )
    (paths.run_dir / "report.json").write_text(
        json.dumps(swiftui_gen.report(result), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if result.errors:
        raise SystemExit(f"codegen failed: {result.errors[:10]}")
    return paths


def _resolve_locale(paths: RunPaths, storefront: str | None) -> str:
    spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
    manifest_path = paths.capture_manifest_json
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else None
    )
    return resolve_source_locale(spec, manifest=manifest, storefront_country=storefront)


def main(argv: list[str] | None = None) -> int:
    """Generate (or reuse) a SwiftUI app and refine it against the Vision Judge."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, help="reuse an existing codegen run")
    parser.add_argument("--app-spec", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--screen", action="append", dest="screens")
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--bundle-id", default="com.iosforge.generated")
    parser.add_argument("--app-name")
    parser.add_argument("--out", type=Path, default=Path("runs"))
    parser.add_argument("--max-parallel", type=int, default=4)
    parser.add_argument("--udid", required=True)
    parser.add_argument("--locale", help="BCP-47; default: resolve_source_locale(spec)")
    parser.add_argument("--storefront", help="App Store country, last-resort locale source")
    args = parser.parse_args(argv)

    if not xcode.toolchain_available():
        print("xcodegen / xcodebuild / xcrun not found: the iOS pipeline only runs on a Mac worker")
        return 2
    if args.run_dir:
        paths = RunPaths.at(args.run_dir)
    else:
        if not (args.app_spec and args.archive):
            parser.error("--app-spec and --archive are required without --run-dir")
        paths = _generate(args)
    settings = get_settings()
    locale = args.locale or _resolve_locale(paths, args.storefront)
    report = compliance.refine_ios_until_complete(
        paths,
        simulator.SimEnvironment(udid=args.udid, locale=locale),
        threshold=settings.frontend_verify_threshold,
        soft_floor=settings.compliance_soft_floor,
        max_iterations=settings.compliance_max_iterations,
        weights=compliance.ComplianceWeights.from_settings(settings),
        blank_max_bytes=settings.web_blank_max_bytes,
        structural_gate=settings.web_verify_hard_gate,
    )
    summary = {
        "run_dir": str(paths.run_dir),
        "compliance_score": report.get("compliance_score"),
        "status": report.get("status"),
        "stop_reason": report.get("stop_reason"),
        "history": report.get("history"),
    }
    print(json.dumps(summary, indent=2))
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
