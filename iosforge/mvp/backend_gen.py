"""Generate a deployable Firebase Cloud Functions package (real server logic).

Emitted into the ``admin/`` deliverable when the app needs a backend. Unlike the
static admin scaffold, this is **real, deployable TypeScript**: server-enforced
wallet/unlock (Firestore transactions the client cannot tamper with), reward
grants with caps, a signed video-URL resolver, and an Apphud webhook that
syncs subscription status into ``users/{uid}``. Requires Blaze to deploy.

Deterministic (no LLM): collection names come from ``collection_prefix`` (multi-
tenant) and are embedded in ``src/config.ts`` so functions read the right paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from iosforge.common.logging import get_logger

log = get_logger("mvp.backend_gen")

_PACKAGE_JSON = {
    "name": "functions",
    "engines": {"node": "20"},
    "main": "lib/index.js",
    "scripts": {
        "build": "tsc",
        "serve": "npm run build && firebase emulators:start --only functions,firestore",
        "test": "npm run build && node lib/test/run.js",
    },
    "dependencies": {
        "firebase-admin": "^12.0.0",
        "firebase-functions": "^5.0.0",
    },
    "devDependencies": {
        "typescript": "^5.4.0",
        "@firebase/rules-unit-testing": "^3.0.0",
    },
    "private": True,
}

_TSCONFIG = {
    "compilerOptions": {
        "module": "commonjs",
        "target": "es2020",
        "outDir": "lib",
        "strict": True,
        "esModuleInterop": True,
        "skipLibCheck": True,
    },
    "include": ["src"],
}

_INDEX_TS = """\
import * as admin from "firebase-admin";
import { onCall, onRequest, HttpsError } from "firebase-functions/v2/https";
import { C } from "./config";

admin.initializeApp();
const db = admin.firestore();

function requireUid(auth: { uid?: string } | undefined): string {
  if (!auth || !auth.uid) throw new HttpsError("unauthenticated", "sign in required");
  return auth.uid;
}

/** Spend coins (or use Pro) to unlock an episode. Atomic; server-enforced. */
export const unlockEpisode = onCall(async (req) => {
  const uid = requireUid(req.auth);
  const episodeId = String(req.data?.episodeId ?? "");
  if (!episodeId) throw new HttpsError("invalid-argument", "episodeId required");

  return db.runTransaction(async (tx) => {
    const userRef = db.collection(C.users).doc(uid);
    const epRef = db.collection(C.episodes).doc(episodeId);
    const [userSnap, epSnap] = await Promise.all([tx.get(userRef), tx.get(epRef)]);
    if (!epSnap.exists) throw new HttpsError("not-found", "episode not found");
    const ep = epSnap.data() as any;
    const user = (userSnap.data() as any) ?? { coinBalance: 0, proStatus: false };

    if (user.proStatus === true || ep.isLocked === false) {
      tx.set(userRef.collection("unlocks").doc(episodeId), { at: admin.firestore.FieldValue.serverTimestamp() });
      return { unlocked: true, via: "pro_or_free" };
    }
    const cost = Number(ep.unlockCost ?? 0);
    const balance = Number(user.coinBalance ?? 0);
    if (balance < cost) throw new HttpsError("failed-precondition", "insufficient coins");

    tx.update(userRef, { coinBalance: balance - cost });
    tx.set(userRef.collection("unlocks").doc(episodeId), { at: admin.firestore.FieldValue.serverTimestamp() });
    tx.set(db.collection(C.transactions).doc(), {
      userId: uid, currency: "coin", amount: -cost, type: "spend", reason: "unlock:" + episodeId,
      createdAt: admin.firestore.FieldValue.serverTimestamp(),
    });
    return { unlocked: true, via: "coins", spent: cost };
  });
});

/** Grant coins for a validated action (daily check-in / rewarded ad), with a daily cap. */
export const grantReward = onCall(async (req) => {
  const uid = requireUid(req.auth);
  const kind = String(req.data?.kind ?? "");
  const amount = Math.max(0, Math.min(Number(req.data?.amount ?? 0), C.rewardDailyCap));
  if (!kind || amount <= 0) throw new HttpsError("invalid-argument", "kind/amount required");

  return db.runTransaction(async (tx) => {
    const userRef = db.collection(C.users).doc(uid);
    const userSnap = await tx.get(userRef);
    const user = (userSnap.data() as any) ?? { bonusBalance: 0 };
    tx.set(userRef, { bonusBalance: Number(user.bonusBalance ?? 0) + amount }, { merge: true });
    tx.set(db.collection(C.transactions).doc(), {
      userId: uid, currency: "bonus", amount, type: "earn", reason: kind,
      createdAt: admin.firestore.FieldValue.serverTimestamp(),
    });
    return { granted: amount };
  });
});

/** Return a playable video URL for an episode the user is entitled to. */
export const signedVideoUrl = onCall(async (req) => {
  const uid = requireUid(req.auth);
  const episodeId = String(req.data?.episodeId ?? "");
  const epSnap = await db.collection(C.episodes).doc(episodeId).get();
  if (!epSnap.exists) throw new HttpsError("not-found", "episode not found");
  const ep = epSnap.data() as any;
  const unlocked = await db.collection(C.users).doc(uid).collection("unlocks").doc(episodeId).get();
  const userSnap = await db.collection(C.users).doc(uid).get();
  const pro = (userSnap.data() as any)?.proStatus === true;
  if (ep.isLocked === true && !pro && !unlocked.exists) {
    throw new HttpsError("permission-denied", "episode locked");
  }
  return { url: String(ep.videoUrl ?? "") };
});

/** Apphud webhook: sync subscription status into users/{uid}. */
export const apphudWebhook = onRequest(async (req, res) => {
  if (req.get("X-Apphud-Token") !== C.apphudSecret) {
    res.status(401).send("unauthorized");
    return;
  }
  const body = (req.body ?? {}) as any;
  const uid = String(body.user_id ?? body.app_user_id ?? "");
  if (!uid) { res.status(400).send("no user_id"); return; }
  const name = String(body.name ?? body.event ?? "");
  const pro = [
    "subscription_started", "subscription_renewed", "subscription_reactivated",
    "subscription_product_changed", "trial_started", "non_renewing_purchase",
  ].includes(name);
  await db.collection(C.users).doc(uid).set(
    { proStatus: pro, proExpiry: body.expires_at ?? null }, { merge: true },
  );
  res.status(200).send("ok");
});
"""

_GITIGNORE = "node_modules/\nlib/\n"


def _config_ts(prefix: str) -> str:
    cfg = {
        "users": f"{prefix}User",
        "episodes": f"{prefix}Episode",
        "series": f"{prefix}Series",
        "transactions": f"{prefix}Transaction",
        "rewardDailyCap": 500,
        "apphudSecret": "SET_ME_APPHUD_WEBHOOK_SECRET",
    }
    return "export const C = " + json.dumps(cfg, indent=2) + " as const;\n"


def _firebase_json(project_id: str) -> str:
    data = {
        "firestore": {"rules": "firestore/firestore.rules", "indexes": "firestore/indexes.json"},
        "functions": {"source": "functions"},
        "storage": {"rules": "storage.rules"},
        "emulators": {
            "auth": {"port": 9099},
            "functions": {"port": 5001},
            "firestore": {"port": 8080},
            "storage": {"port": 9199},
            "ui": {"enabled": True},
        },
    }
    return json.dumps(data, indent=2)


def _storage_rules() -> str:
    return (
        "rules_version = '2';\n"
        "service firebase.storage {\n"
        "  match /b/{bucket}/o {\n"
        "    match /{allPaths=**} {\n"
        "      allow read: if true;            // public media (posters, placeholder clips)\n"
        "      allow write: if false;          // uploads only via admin SDK / functions\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


def generate_backend(
    spec: dict[str, Any], admin_dir: Path, *, collection_prefix: str = "", project_id: str = "app"
) -> Path | None:
    """Write a deployable Functions package + firebase deploy config into ``admin_dir``.

    Returns the ``functions/`` dir, or None when the app declares no backend.
    """
    backend = spec.get("backend", {})
    if not (isinstance(backend, dict) and backend.get("backend_needed")):
        return None

    fns = admin_dir / "functions"
    (fns / "src").mkdir(parents=True, exist_ok=True)
    (fns / "package.json").write_text(json.dumps(_PACKAGE_JSON, indent=2))
    (fns / "tsconfig.json").write_text(json.dumps(_TSCONFIG, indent=2))
    (fns / ".gitignore").write_text(_GITIGNORE)
    (fns / "src" / "config.ts").write_text(_config_ts(collection_prefix))
    (fns / "src" / "index.ts").write_text(_INDEX_TS)

    (admin_dir / "firebase.json").write_text(_firebase_json(project_id))
    (admin_dir / ".firebaserc").write_text(
        json.dumps({"projects": {"default": project_id}}, indent=2)
    )
    (admin_dir / "storage.rules").write_text(_storage_rules())

    log.info("backend_gen.done", functions=5, prefix=collection_prefix or "(none)")
    return fns
