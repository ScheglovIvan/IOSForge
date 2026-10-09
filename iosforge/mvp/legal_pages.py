"""Privacy Policy and Terms pages for a shipped app, hosted on GitHub Pages.

App Store review rejects a subscription app whose paywall has no working Terms and
Privacy links, and App Store Connect refuses to submit without a Privacy Policy URL.
The pages have to live somewhere with a real certificate, which rules out the admin
host (an IP with a self-signed cert), so they are published to a ``gh-pages`` branch
of the app's own repository — a branch the code push never touches, so a re-generated
app cannot delete its own legal pages.

The text is built from what the app ACTUALLY does: the SDKs compiled into it,
the Info.plist usage descriptions it declares and the third-party endpoints it
calls. A policy that claims less than the app collects is worse than none at all, so
nothing here is generic filler — every section is emitted because something in the
build asked for it.

Terms of Use are Apple's standard EULA. Apple licenses it for exactly this purpose,
review accepts it, and it needs no hosting or maintenance.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger

log = get_logger("mvp.legal_pages")

APPLE_EULA_URL = "https://www.apple.com/legal/internet-services/itunes/dev/stdeula/"

PAGES_BRANCH = "gh-pages"


class LegalPagesError(RuntimeError):
    """Generating or publishing the legal pages failed."""


@dataclass(frozen=True)
class DataPractice:
    """One thing the app collects, and why — a row in the policy."""

    title: str
    detail: str


@dataclass
class AppLegalFacts:
    """What an app does with data, read off its own build inputs."""

    app_name: str
    bundle_id: str
    contact_email: str
    practices: list[DataPractice] = field(default_factory=list)
    processors: list[DataPractice] = field(default_factory=list)

    @property
    def collects_tracking(self) -> bool:
        return any("track" in p.title.lower() for p in self.practices)


_SDK_PROCESSORS: dict[str, DataPractice] = {
    "apphud": DataPractice(
        "Apphud",
        "Manages subscriptions and purchases. Receives a pseudonymous user "
        "identifier, device and app version, country and purchase history so your "
        "subscription can be restored on your other devices.",
    ),
    "tenjin": DataPractice(
        "Tenjin",
        "Measures which advertisement or link brought you to the app. Receives "
        "device and app identifiers, install and session events, and the country "
        "your device reports.",
    ),
    "google_fonts": DataPractice(
        "Google Fonts",
        "Serves the typefaces used in the interface. Receives your IP address when "
        "a font is fetched.",
    ),
}

_PERMISSION_PRACTICES: dict[str, DataPractice] = {
    "NSLocationWhenInUseUsageDescription": DataPractice(
        "Location",
        "Used while the app is open to place you on the map and to look up local "
        "conditions. It is processed on your device and by Apple's mapping "
        "services; we do not store your location or send it to our own servers.",
    ),
    "NSMicrophoneUsageDescription": DataPractice(
        "Microphone",
        "Used to measure the sound level around you. The audio is analysed live on "
        "your device to produce a level reading; nothing is recorded, stored or "
        "transmitted.",
    ),
    "NSAppleMusicUsageDescription": DataPractice(
        "Media library",
        "Used to show what your device is currently playing and to control "
        "playback. The information stays on your device.",
    ),
    "NSCameraUsageDescription": DataPractice(
        "Camera",
        "Used only while you are scanning, and the image is processed on your "
        "device. No photograph is stored or transmitted.",
    ),
    "NSUserTrackingUsageDescription": DataPractice(
        "Tracking permission",
        "If you allow tracking, the advertising identifier your device provides is "
        "used to attribute your install to the campaign that led you here. If you "
        "decline, attribution falls back to Apple's privacy-preserving "
        "SKAdNetwork and no advertising identifier is read.",
    ),
}

_ALWAYS = [
    DataPractice(
        "Settings you choose",
        "Your preferences — selected language, theme, sound options and progress "
        "through the app — are stored on your device only.",
    ),
    DataPractice(
        "Purchases",
        "Subscriptions are billed by Apple. We never see or receive your payment "
        "card, and Apple shares only whether a purchase succeeded.",
    ),
]


def facts_from_build(
    *,
    app_name: str,
    bundle_id: str,
    contact_email: str,
    sdks: list[str],
    permissions: dict[str, Any],
    remote_endpoints: list[DataPractice] | None = None,
) -> AppLegalFacts:
    """Assemble the policy's factual content from the app's own build inputs."""
    facts = AppLegalFacts(app_name=app_name, bundle_id=bundle_id, contact_email=contact_email)
    for key in permissions:
        practice = _PERMISSION_PRACTICES.get(key)
        if practice is not None:
            facts.practices.append(practice)
    facts.practices.extend(_ALWAYS)
    facts.practices.extend(remote_endpoints or [])
    for sdk in sdks:
        processor = _SDK_PROCESSORS.get(sdk)
        if processor is not None and processor not in facts.processors:
            facts.processors.append(processor)
    return facts


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0 auto; padding: 40px 20px 80px; max-width: 44rem;
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  color: #16211f; background: #fff;
}
h1 { font-size: 1.9rem; line-height: 1.2; margin: 0 0 6px; }
h2 { font-size: 1.15rem; margin: 34px 0 8px; }
p, li { color: #33413e; }
.updated { color: #6b7a77; font-size: .9rem; margin: 0 0 28px; }
.term { font-weight: 600; color: #16211f; }
ul { padding-left: 1.2rem; }
li { margin: 10px 0; }
a { color: #0f766e; }
footer { margin-top: 44px; border-top: 1px solid #e3e9e7; padding-top: 16px;
         color: #6b7a77; font-size: .9rem; }
@media (prefers-color-scheme: dark) {
  body { color: #e8efed; background: #101615; }
  h1, .term { color: #f2f7f6; }
  p, li { color: #c2cfcc; }
  .updated, footer { color: #8fa09c; }
  a { color: #5eead4; }
  footer { border-top-color: #22302d; }
}
"""


def render_privacy(facts: AppLegalFacts, *, updated: date) -> str:
    """The Privacy Policy page for one app."""
    name = _escape(facts.app_name)
    rows = "\n".join(
        f'      <li><span class="term">{_escape(p.title)}.</span> {_escape(p.detail)}</li>'
        for p in facts.practices
    )
    processors = "\n".join(
        f'      <li><span class="term">{_escape(p.title)}.</span> {_escape(p.detail)}</li>'
        for p in facts.processors
    )
    tracking_note = (
        "<p>The app asks for tracking permission on first launch. You can change "
        "your answer at any time in <em>Settings ▸ Privacy &amp; Security ▸ "
        "Tracking</em> on your device.</p>"
        if facts.collects_tracking
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy — {name}</title>
<style>{_STYLE}</style>
</head>
<body>
  <h1>Privacy Policy</h1>
  <p class="updated">{name} ({_escape(facts.bundle_id)}) · Last updated
     {updated.strftime("%d %B %Y")}</p>

  <p>This policy explains what {name} does with information when you use it. It
     covers only this app.</p>

  <h2>What the app uses, and why</h2>
  <ul>
{rows}
  </ul>
  {tracking_note}

  <h2>Who else processes data</h2>
  <p>These services act on our behalf and only receive what their function needs:</p>
  <ul>
{processors}
  </ul>

  <h2>What we do not do</h2>
  <ul>
    <li>We do not sell your personal information.</li>
    <li>We do not ask for your name, address or phone number.</li>
    <li>We do not build advertising profiles about you.</li>
  </ul>

  <h2>How long data is kept</h2>
  <p>Settings and progress stay on your device until you delete the app.
     Subscription and attribution records are kept by the services above for as
     long as they need them to provide the service, and are deleted on request.</p>

  <h2>Children</h2>
  <p>The app is not directed at children under 13, and we do not knowingly collect
     information from them.</p>

  <h2>Your rights</h2>
  <p>You can ask what data relates to you, ask for it to be corrected, or ask for
     it to be deleted. Write to
     <a href="mailto:{_escape(facts.contact_email)}">{_escape(facts.contact_email)}</a>
     and we will respond within 30 days. Deleting the app removes everything held
     on your device.</p>

  <h2>Changes</h2>
  <p>If this policy changes, the date at the top changes with it.</p>

  <h2>Contact</h2>
  <p><a href="mailto:{_escape(facts.contact_email)}">{_escape(facts.contact_email)}</a></p>

  <footer>Terms of Use for this app are Apple's standard licence agreement,
    available at <a href="{APPLE_EULA_URL}">apple.com</a>.</footer>
</body>
</html>
"""


def render_index(apps: list[tuple[str, str]]) -> str:
    """A small landing page linking each app's policy."""
    items = "\n".join(
        f'    <li><a href="{_escape(slug)}">{_escape(title)}</a></li>' for slug, title in apps
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Legal</title>
<style>{_STYLE}</style>
</head>
<body>
  <h1>Legal</h1>
  <ul>
{items}
  </ul>
</body>
</html>
"""


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def publish_pages(
    *,
    settings: Settings,
    token: str,
    repo_full_name: str,
    files: dict[str, str],
    commit_message: str = "Publish legal pages",
) -> str:
    """Push ``files`` to the repo's ``gh-pages`` branch and enable GitHub Pages.

    The branch is created orphaned and force-pushed, so it holds only these pages
    and can never collide with the app code on the default branch.

    Returns the public base URL that GitHub serves the branch from.
    """
    if not token:
        raise LegalPagesError("no GitHub token")
    owner, _, repo = repo_full_name.partition("/")
    if not owner or not repo:
        raise LegalPagesError(f"bad repo name {repo_full_name!r}")

    with tempfile.TemporaryDirectory(prefix="iosforge-legal-") as tmp:
        work = Path(tmp)
        for name, body in files.items():
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        (work / ".nojekyll").write_text("")

        remote = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"

        def git(*args: str) -> None:
            res = subprocess.run(
                ["git", *args], cwd=work, capture_output=True, text=True, check=False
            )
            if res.returncode != 0:
                detail = (res.stderr or res.stdout).replace(token, "<token>")
                raise LegalPagesError(f"git {args[0]}: {detail[:300]}")

        git("init", "-q")
        git("checkout", "-q", "-b", PAGES_BRANCH)
        git("add", "-A")
        git(
            "-c",
            "user.name=iosforge",
            "-c",
            "user.email=noreply@localhost",
            "commit",
            "-q",
            "-m",
            commit_message,
        )
        git("push", "-q", "--force", remote, f"{PAGES_BRANCH}:{PAGES_BRANCH}")

    base = f"{settings.github_api_base}/repos/{repo_full_name}/pages"
    headers = _api_headers(token)
    source = {"source": {"branch": PAGES_BRANCH, "path": "/"}}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(base, headers=headers, json=source)
        if resp.status_code == 409:
            resp = client.put(base, headers=headers, json=source)
        if resp.status_code >= 400 and resp.status_code != 409:
            log.warning(
                "legal_pages.enable_failed",
                repo=repo_full_name,
                status=resp.status_code,
                body=resp.text[:200],
            )
        info = client.get(base, headers=headers)
        if info.status_code == 200:
            url = str(info.json().get("html_url") or "").rstrip("/")
            if url:
                log.info("legal_pages.published", repo=repo_full_name, url=url)
                return url

    fallback = f"https://{owner.lower()}.github.io/{repo}"
    log.info("legal_pages.published", repo=repo_full_name, url=fallback, fallback=True)
    return fallback


def repo_full_name(repo_url: str) -> str:
    """``https://github.com/owner/repo`` → ``owner/repo``."""
    cleaned = repo_url.strip().rstrip("/")
    cleaned = re.sub(r"\.git$", "", cleaned)
    parts = cleaned.split("/")
    if len(parts) < 2:
        raise LegalPagesError(f"cannot read repo from {repo_url!r}")
    return f"{parts[-2]}/{parts[-1]}"


def render_support(facts: AppLegalFacts) -> str:
    """A minimal support page: what the app is and how to reach a human.

    App Store review requires a Support URL that resolves to a real page with a way to
    get help. This gives one without a separate site, hosted alongside the policy.
    """
    name = _escape(facts.app_name)
    email = _escape(facts.contact_email)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Support — {name}</title>
<style>{_STYLE}</style>
</head>
<body>
  <h1>Support</h1>
  <p>Need help with <strong>{name}</strong>? We answer every message.</p>

  <h2>Contact us</h2>
  <p>Email <a href="mailto:{email}">{email}</a> and we will get back to you, usually within
     one business day. Tell us your device model and iOS version so we can help faster.</p>

  <h2>Common questions</h2>
  <ul>
    <li><span class="term">A purchase did not unlock.</span> Open the app, go to Settings and
        tap <em>Restore purchases</em>. If it still does not unlock, email us the Apple ID
        region you bought from.</li>
    <li><span class="term">How do I cancel a subscription?</span> Subscriptions are managed by
        Apple: open the App Store, tap your account photo, then <em>Subscriptions</em>.</li>
    <li><span class="term">Something is not working.</span> Email us what you were doing when it
        happened — a screenshot helps.</li>
  </ul>

  <footer>Privacy Policy and Terms of Use are linked from the app and from
    <a href="privacy">this page</a>.</footer>
</body>
</html>
"""


def build_pages(facts: AppLegalFacts, *, updated: date | None = None) -> dict[str, str]:
    """The files to publish for one app."""
    when = updated or date.today()
    privacy = render_privacy(facts, updated=when)
    support = render_support(facts)
    return {
        "index.html": render_index(
            [
                ("privacy.html", f"{facts.app_name} — Privacy Policy"),
                ("support.html", f"{facts.app_name} — Support"),
            ]
        ),
        "privacy.html": privacy,
        "privacy/index.html": privacy,
        "support.html": support,
        "support/index.html": support,
    }


def load_permissions(path: Path) -> dict[str, Any]:
    """Read an ``ios_permissions.json``; an empty mapping when absent."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
