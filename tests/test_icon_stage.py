"""Staging the chosen icon into the Flutter project the build compiles."""

from __future__ import annotations

from pathlib import Path

from iosforge.mvp import icon_stage

_PUBSPEC = """\
name: demo
environment:
  sdk: ">=3.4.0 <4.0.0"

dependencies:
  flutter:
    sdk: flutter

dev_dependencies:
  flutter_test:
    sdk: flutter
  flutter_lints: ^4.0.0

flutter:
  uses-material-design: true
  assets:
    - assets/
    - assets/audio/
"""


def _project(tmp_path: Path, pubspec: str = _PUBSPEC) -> Path:
    app = tmp_path / "flutter_app"
    app.mkdir(parents=True, exist_ok=True)
    (app / "pubspec.yaml").write_text(pubspec, encoding="utf-8")
    return app


def test_icon_lands_where_the_build_expects_it(tmp_path: Path) -> None:
    app = _project(tmp_path)
    assert icon_stage.stage(app, b"PNGDATA") is True
    assert (app / icon_stage.ICON_PATH).read_bytes() == b"PNGDATA"


def test_asset_dir_is_registered_so_dart_can_show_the_mark(tmp_path: Path) -> None:
    # without this the splash / paywall cannot load the icon at runtime
    app = _project(tmp_path)
    icon_stage.stage(app, b"PNGDATA")
    text = (app / "pubspec.yaml").read_text()
    assert f"- {icon_stage.ASSET_DIR}" in text
    assert "- assets/audio/" in text  # existing assets survive


def test_launcher_tool_is_added_and_configured(tmp_path: Path) -> None:
    app = _project(tmp_path)
    icon_stage.stage(app, b"PNGDATA")
    text = (app / "pubspec.yaml").read_text()
    assert f"  {icon_stage.LAUNCHER_PACKAGE}: {icon_stage.LAUNCHER_VERSION}" in text
    assert f"\n{icon_stage.LAUNCHER_PACKAGE}:" in text
    assert f'image_path: "{icon_stage.ICON_PATH}"' in text


def test_ios_alpha_is_flattened_because_app_store_rejects_it(tmp_path: Path) -> None:
    app = _project(tmp_path)
    icon_stage.stage(app, b"PNGDATA")
    assert "remove_alpha_ios: true" in (app / "pubspec.yaml").read_text()


def test_staging_twice_leaves_the_pubspec_unchanged(tmp_path: Path) -> None:
    # a rebuild after an icon re-roll must swap the PNG, not duplicate config
    app = _project(tmp_path)
    icon_stage.stage(app, b"FIRST")
    once = (app / "pubspec.yaml").read_text()
    icon_stage.stage(app, b"SECOND")
    assert (app / "pubspec.yaml").read_text() == once
    assert (app / icon_stage.ICON_PATH).read_bytes() == b"SECOND"


def test_a_project_without_an_assets_block_still_gets_the_launcher(tmp_path: Path) -> None:
    app = _project(tmp_path, "name: demo\n\ndev_dependencies:\n  flutter_test:\n    sdk: flutter\n")
    assert icon_stage.stage(app, b"PNGDATA") is True
    assert icon_stage.LAUNCHER_PACKAGE in (app / "pubspec.yaml").read_text()


def test_no_icon_and_no_project_are_both_no_ops(tmp_path: Path) -> None:
    # a job whose icon was never generated must build with the Flutter default
    assert icon_stage.stage(_project(tmp_path), b"") is False
    assert icon_stage.stage(tmp_path / "absent", b"PNGDATA") is False
