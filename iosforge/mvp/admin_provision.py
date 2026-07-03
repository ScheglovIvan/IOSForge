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


class _FirebaseClient:
    """Thin wrapper over the Firebase/Google admin SDKs (lazy-imported)."""

    def __init__(self, cred: Any, sa_path: str, project_id_hint: str, create_project: bool) -> None:
        self._cred = cred
        self._sa_path = sa_path
        self._hint = project_id_hint
        self._create = create_project

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
        return cls(cred, str(sa), settings.firebase_project_id, settings.firebase_create_project)

    def ensure_project(self) -> str:
        if not self._create:
            if not self._hint:
                raise ProvisionError(
                    "firebase_project_id is empty and firebase_create_project is false"
                )
            return self._hint
        from googleapiclient.discovery import build

        fb = build("firebase", "v1beta1", credentials=self._cred, cache_discovery=False)
        project_id = self._hint or "iosforge-app"
        op = fb.projects().addFirebase(project=f"projects/{project_id}", body={}).execute()
        log.info("admin_provision.addFirebase", project=project_id, op=op.get("name"))
        return project_id

    def ensure_firestore(self, project_id: str, location: str = "nam5") -> None:
        from googleapiclient.discovery import build

        fs = build("firestore", "v1", credentials=self._cred, cache_discovery=False)
        try:
            fs.projects().databases().create(
                parent=f"projects/{project_id}",
                databaseId="(default)",
                body={"type": "FIRESTORE_NATIVE", "locationId": location},
            ).execute()
        except Exception as exc:  # already exists / permission — non-fatal
            log.info("admin_provision.firestore_exists_or_skip", error=str(exc)[:200])

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
        rules.projects().releases().create(
            name=f"projects/{project_id}",
            body={
                "release": {
                    "name": f"projects/{project_id}/releases/cloud.firestore",
                    "rulesetName": ruleset["name"],
                }
            },
        ).execute()
        return True

    def seed(
        self, project_id: str, collections: dict[str, dict[str, str]], per: int = 3
    ) -> dict[str, int]:
        import firebase_admin
        from firebase_admin import credentials, firestore

        app = firebase_admin.initialize_app(
            credentials.Certificate(self._sa_path),
            {"projectId": project_id},
            name=f"seed-{project_id}",
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

    client = _build_client(settings)
    project_id = client.ensure_project()
    client.ensure_firestore(project_id)
    rules_deployed = client.deploy_rules(project_id, rules_text) if rules_text else False
    seeded = client.seed(project_id, collections)

    result: dict[str, Any] = {
        "provider": "firebase_rowy",
        "project_id": project_id,
        "console_url": f"https://console.firebase.google.com/project/{project_id}",
        "rules_deployed": rules_deployed,
        "seeded": seeded,
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
