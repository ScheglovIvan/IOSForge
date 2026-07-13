"""GitHub Upload stage: repo-name slugging, token loading, publish orchestration."""

from __future__ import annotations

from pathlib import Path

import pytest

from iosforge.common.config import Settings
from iosforge.mvp import github_publish


def test_slugify_repo() -> None:
    assert github_publish.slugify_repo("Silly Fun Smile Live Wallpaper") == (
        "silly-fun-smile-live-wallpaper"
    )
    assert github_publish.slugify_repo("  App!! 2.0 ") == "app-2-0"
    assert github_publish.slugify_repo("") == "app"


def test_load_token_from_file(tmp_path: Path) -> None:
    f = tmp_path / "github.env"
    f.write_text("# token\nGITHUB_TOKEN=ghp_abc123\n")
    s = Settings(github_token_path=str(f))
    assert github_publish.load_token(s) == "ghp_abc123"


def test_publish_requires_token(tmp_path: Path) -> None:
    (tmp_path / "proj").mkdir()
    s = Settings(github_token_path="")  # no token
    with pytest.raises(github_publish.GitHubPublishError, match="no GitHub token"):
        github_publish.publish(s, tmp_path / "proj", app_name="X")


def test_publish_orchestrates_repo_and_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pubspec.yaml").write_text("name: app")
    tokf = tmp_path / "github.env"
    tokf.write_text("GITHUB_TOKEN=ghp_xyz\n")
    s = Settings(github_token_path=str(tokf), github_repo_prefix="")

    monkeypatch.setattr(
        github_publish,
        "_create_repo",
        lambda settings, token, name, desc: {
            "name": name,
            "clone_url": f"https://github.com/me/{name}.git",
            "html_url": f"https://github.com/me/{name}",
            "full_name": f"me/{name}",
        },
    )
    git_calls: list[list[str]] = []
    monkeypatch.setattr(
        github_publish, "_run_git", lambda args, cwd, token_url=None: git_calls.append(args)
    )

    result = github_publish.publish(s, proj, app_name="My Cool App", description="a demo")

    assert result["url"] == "https://github.com/me/my-cool-app"
    assert result["name"] == "my-cool-app"
    # full git sequence: init -> config x2 -> add -> commit -> push
    verbs = [c[0] for c in git_calls]
    assert verbs == ["init", "config", "config", "add", "commit", "push"]
    # the token never appears in the recorded (sanitized) command list
    assert not any("ghp_xyz" in " ".join(c) for c in git_calls if c[0] != "push")


def test_clean_project_prunes_artifacts_and_keeps_assets(tmp_path: Path) -> None:
    from iosforge.mvp.github_publish import _clean_project

    proj = tmp_path / "app"
    (proj / "build" / "web").mkdir(parents=True)
    (proj / ".dart_tool").mkdir()
    (proj / "assets" / "media").mkdir(parents=True)
    (proj / "assets" / "media" / "clip.mp4").write_bytes(b"x")
    (proj / "lib").mkdir()
    (proj / "lib" / "main.dart").write_text("void main() {}")
    (proj / "app_spec.json").write_text("{}")
    (proj / "TASK.md").write_text("internal")
    (proj / "pubspec.yaml").write_text("name: app")

    _clean_project(proj)

    assert not (proj / "build").exists()
    assert not (proj / ".dart_tool").exists()
    assert not (proj / "app_spec.json").exists()
    assert not (proj / "TASK.md").exists()
    assert (proj / "assets" / "media" / "clip.mp4").exists()  # embedded assets kept
    assert (proj / "lib" / "main.dart").exists()
    assert (proj / ".gitignore").read_text().find("build/") != -1


def test_push_existing_force_pushes_new_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "app"
    (proj / "lib").mkdir(parents=True)
    (proj / "pubspec.yaml").write_text("name: app")
    tokf = tmp_path / "github.env"
    tokf.write_text("GITHUB_TOKEN=ghp_zzz\n")
    s = Settings(github_token_path=str(tokf))

    calls: list[list[str]] = []
    monkeypatch.setattr(
        github_publish, "_run_git", lambda args, cwd, token_url=None: calls.append(args)
    )
    res = github_publish.push_existing(s, proj, full_name="me/app", message="Rework v2")

    assert res["url"] == "https://github.com/me/app"
    verbs = [c[0] for c in calls]
    assert verbs == ["init", "config", "config", "add", "commit", "push"]
    assert calls[-1][:2] == ["push", "--force"]  # snapshot force-push
    assert calls[-2] == ["commit", "-m", "Rework v2"]
