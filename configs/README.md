# configs/

Runtime configuration that can change **without a redeploy** (SPEC §9):

- provider selection (catalog / apk / emulator / codegen / promptset),
- APK source resources with priorities and fallbacks,
- thresholds and limits for pipeline stages.

Loading and validation of these configs (config layer / structlog wiring) is
implemented in task T-1.3 — this directory is only the placeholder location.
