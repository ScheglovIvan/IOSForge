"""Provision the generated admin backend on a live Firebase project.

Consumes the ``admin/`` deliverable from :mod:`iosforge.mvp.admin_gen` and, using
a Google service-account key (path in ``Settings.firebase_sa_path``), ensures a
Firebase project + Firestore, deploys the security rules and seeds every
collection with placeholder documents. The real Google/Firebase SDK calls are
isolated in :class:`_FirebaseClient` (lazy-imported) so the orchestration is
unit-testable without credentials. Opt-in: runs only when ``provision_admin`` and
``firebase_sa_path`` are set. Untested against live Firebase until creds arrive.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger

log = get_logger("mvp.admin_provision")

_PLACEHOLDER = {
    "string": "PLACEHOLDER",
    "number": 0,
    "boolean": False,
    "timestamp": None,
    "array": [],
}


class ProvisionError(RuntimeError):
    """Raised when provisioning cannot proceed (missing creds, API failure)."""


def make_project_id(app_name: str, prefix: str) -> str:
    """Build a globally-unique GCP project id: ``{prefix}-{slug}-{rand6}``.

    Conforms to GCP rules: 6–30 chars, lowercase ``[a-z][a-z0-9-]*[a-z0-9]``.
    The 6-char random suffix is reserved before truncation so it is never lost —
    it is the only global-uniqueness guarantee across clones of the same app.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (app_name or "app").lower()).strip("-") or "app"
    pref = re.sub(r"[^a-z0-9]+", "-", (prefix or "iosforge").lower()).strip("-") or "iosforge"
    if not pref[0].isalpha():
        pref = f"a{pref}"
    rand = uuid.uuid4().hex[:6]
    head = f"{pref}-{slug}"[:23].strip("-")
    return f"{head}-{rand}"


class _FirebaseClient:
    """Thin wrapper over the Firebase/Google admin SDKs (lazy-imported)."""

    def __init__(
        self,
        cred: Any,
        sa_path: str,
        project_id_hint: str,
        create_project: bool,
        *,
        parent: str = "",
        prefix: str = "iosforge",
    ) -> None:
        self._cred = cred
        self._sa_path = sa_path
        self._hint = project_id_hint
        self._create = create_project
        self._parent = parent
        self._prefix = prefix

    @classmethod
    def from_settings(cls, settings: Settings) -> _FirebaseClient:
        if not settings.firebase_sa_path:
            raise ProvisionError("firebase_sa_path is empty — set it in .env")
        sa = Path(settings.firebase_sa_path)
        if not sa.is_file():
            raise ProvisionError(f"service-account key not found: {sa}")
        from google.oauth2 import service_account

        cred = service_account.Credentials.from_service_account_file(
            str(sa), scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return cls(
            cred,
            str(sa),
            settings.firebase_project_id,
            settings.firebase_create_project,
            parent=settings.firebase_parent,
            prefix=settings.firebase_project_prefix,
        )

    @staticmethod
    def _poll_operation(
        service: Any, op_name: str, *, attempts: int = 60, delay: int = 5
    ) -> dict[str, Any]:
        """Poll a long-running operation until done; return the final operation."""
        if not op_name:
            return {}
        op: dict[str, Any] = {}
        for _ in range(attempts):
            op = service.operations().get(name=op_name).execute()
            if op.get("done"):
                if op.get("error"):
                    raise ProvisionError(f"operation failed: {op['error']}")
                return op
            time.sleep(delay)
        log.warning("admin_provision.operation_timeout", op=op_name)
        return op

    def ensure_project(self, app_name: str = "app") -> str:
        if not self._create:
            if not self._hint:
                raise ProvisionError(
                    "firebase_project_id is empty and firebase_create_project is false"
                )
            return self._hint
        from googleapiclient.discovery import build

        project_id = self._hint or make_project_id(app_name, self._prefix)

        crm = build("cloudresourcemanager", "v3", credentials=self._cred, cache_discovery=False)
        body: dict[str, Any] = {"projectId": project_id, "displayName": project_id}
        if self._parent:
            body["parent"] = self._parent
        else:
            log.warning("admin_provision.no_parent", project=project_id)
        op = crm.projects().create(body=body).execute()
        self._poll_operation(crm, op.get("name", ""))
        log.info("admin_provision.project_created", project=project_id)

        fb = build("firebase", "v1beta1", credentials=self._cred, cache_discovery=False)
        op = fb.projects().addFirebase(project=f"projects/{project_id}", body={}).execute()
        self._poll_operation(fb, op.get("name", ""))
        log.info("admin_provision.addFirebase", project=project_id)
        return project_id

    def ensure_billing(self, project_id: str) -> bool:
        """Link a Cloud Billing account (Blaze). Stub until the billing step —
        returns ``False`` (Spark). Storage content + Functions deploy gate on this."""
        log.info("admin_provision.billing_skipped", project=project_id)
        return False

    def ensure_web_app(self, project_id: str, display_name: str) -> dict[str, str]:
        """Ensure a Firebase Web App exists and return its client config (idempotent)."""
        from googleapiclient.discovery import build

        fb = build("firebase", "v1beta1", credentials=self._cred, cache_discovery=False)
        parent = f"projects/{project_id}"

        def _first_app() -> str:
            apps = fb.projects().webApps().list(parent=parent).execute().get("apps", [])
            return str(apps[0]["name"]) if apps else ""

        app_res = _first_app()
        if not app_res:
            op = (
                fb.projects()
                .webApps()
                .create(parent=parent, body={"displayName": display_name})
                .execute()
            )
            final = self._poll_operation(fb, op.get("name", ""))
            app_res = str(final.get("response", {}).get("name", ""))
            for _ in range(6):
                if app_res:
                    break
                time.sleep(5)
                app_res = _first_app()
        if not app_res:
            raise ProvisionError(f"web app not found after create for {project_id}")
        cfg = fb.projects().webApps().getConfig(name=f"{app_res}/config").execute()
        cfg["_appResource"] = app_res
        return {str(k): str(v) for k, v in cfg.items() if v is not None}

    def ensure_firestore(self, project_id: str, location: str = "nam5") -> None:
        from googleapiclient.discovery import build

        su = build("serviceusage", "v1", credentials=self._cred, cache_discovery=False)
        try:
            su.services().enable(
                name=f"projects/{project_id}/services/firestore.googleapis.com", body={}
            ).execute()
        except Exception as exc:  # already enabled — non-fatal
            log.info("admin_provision.firestore_api_enable_skip", error=str(exc)[:200])

        fs = build("firestore", "v1", credentials=self._cred, cache_discovery=False)

        def _has_default() -> bool:
            for _ in range(6):
                try:
                    dbs = fs.projects().databases().list(parent=f"projects/{project_id}").execute()
                    return any(
                        d.get("name", "").endswith("/(default)") for d in dbs.get("databases", [])
                    )
                except Exception:  # API still propagating after enable
                    time.sleep(5)
            return False

        if _has_default():
            return
        try:
            fs.projects().databases().create(
                parent=f"projects/{project_id}",
                databaseId="(default)",
                body={"type": "FIRESTORE_NATIVE", "locationId": location},
            ).execute()
        except Exception as exc:  # concurrent create / already exists — non-fatal
            log.info("admin_provision.firestore_create_skip", error=str(exc)[:200])
        for _ in range(30):
            if _has_default():
                return
            time.sleep(5)
        log.warning("admin_provision.firestore_not_ready", project=project_id)

    def deploy_rules(self, project_id: str, rules_text: str) -> bool:
        from googleapiclient.discovery import build

        rules = build("firebaserules", "v1", credentials=self._cred, cache_discovery=False)
        ruleset = (
            rules.projects()
            .rulesets()
            .create(
                name=f"projects/{project_id}",
                body={"source": {"files": [{"name": "firestore.rules", "content": rules_text}]}},
            )
            .execute()
        )
        release_name = f"projects/{project_id}/releases/cloud.firestore"
        release = {"name": release_name, "rulesetName": ruleset["name"]}
        try:
            rules.projects().releases().create(
                name=f"projects/{project_id}", body=release
            ).execute()
        except Exception:  # release already exists — point it at the new ruleset
            rules.projects().releases().patch(name=release_name, body=release).execute()
        return True

    def seed(
        self, project_id: str, collections: dict[str, dict[str, str]], per: int = 3
    ) -> dict[str, int]:
        import firebase_admin
        from firebase_admin import credentials, firestore

        app = firebase_admin.initialize_app(
            credentials.Certificate(self._sa_path),
            {"projectId": project_id},
            name=f"seed-{project_id}-{uuid.uuid4().hex}",
        )
        db = firestore.client(app)
        counts: dict[str, int] = {}
        for name, fields in collections.items():
            for _ in range(per):
                doc = {k: _PLACEHOLDER.get(t, "PLACEHOLDER") for k, t in fields.items()}
                db.collection(name).add(doc)
            counts[name] = per
        return counts


def _build_client(settings: Settings) -> _FirebaseClient:
    return _FirebaseClient.from_settings(settings)


def provision(admin_dir: Path, settings: Settings) -> dict[str, Any]:
    """Provision the live Firebase backend from the ``admin/`` deliverable."""
    schema_path = admin_dir / "firestore" / "collections.schema.json"
    if not schema_path.is_file():
        raise ProvisionError(f"admin deliverable incomplete: missing {schema_path}")
    collections = json.loads(schema_path.read_text()).get("collections", {})
    rules_path = admin_dir / "firestore" / "firestore.rules"
    rules_text = rules_path.read_text() if rules_path.is_file() else ""

    manifest = {}
    manifest_path = admin_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
    prefix = str(manifest.get("collection_prefix", ""))
    app_name = str(manifest.get("app_name", "App"))

    client = _build_client(settings)
    project_id = client.ensure_project(app_name)
    billing_enabled = client.ensure_billing(project_id)
    client.ensure_firestore(project_id)
    rules_deployed = client.deploy_rules(project_id, rules_text) if rules_text else False
    firebase_config = client.ensure_web_app(project_id, app_name)
    web_app_id = firebase_config.get("appId", "")
    seeded = client.seed(project_id, collections)

    content: dict[str, Any] | None = None
    wants_content = settings.seed_placeholder_content and any(
        str(c).endswith("Episode") for c in collections
    )
    if wants_content and not billing_enabled:
        log.info("admin_provision.content_skipped_no_billing", project=project_id)
    elif wants_content:
        from iosforge.mvp import content_seed

        try:
            content = content_seed.seed_content(
                settings.firebase_sa_path,
                project_id,
                collection_prefix=prefix,
                work_dir=admin_dir / "_content",
                spec={"app_name": app_name},
            )
        except Exception as exc:  # Storage needs Blaze — non-fatal without billing
            log.warning("admin_provision.content_seed_failed", project=project_id, error=str(exc))

    result: dict[str, Any] = {
        "provider": "firebase_rowy",
        "project_id": project_id,
        "console_url": f"https://console.firebase.google.com/project/{project_id}",
        "rules_deployed": rules_deployed,
        "seeded": seeded,
        "billing_enabled": billing_enabled,
        "web_app_id": web_app_id,
        "firebase_config": firebase_config,
        "content": content,
    }
    (admin_dir / "provision_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False)
    )
    log.info(
        "admin_provision.done",
        project_id=project_id,
        rules_deployed=rules_deployed,
        collections=len(seeded),
    )
    return result
