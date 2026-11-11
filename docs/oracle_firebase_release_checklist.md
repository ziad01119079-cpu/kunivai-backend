# Oracle and Firebase release checklist

This phase is designed for local review first. No production change is implied
by this checklist.

## Current routing evidence (read-only review)

Firebase Remote Config project `kuwaitchat-ai-project`, template version 73,
routes `CHAT`, `UPLOAD_URL`, `ASSETS_URL`, `SCHEDULE_IMPORT_URL`, and
`NOTIFICATIONS_BASE_URL` to `https://api.kunivai.online`. The Flutter role
endpoint is derived from `NOTIFICATIONS_BASE_URL`, so it resolves to
`https://api.kunivai.online/admin/users/{uid}/role`.

`CLOUD_FUNCTIONS_BASE_URL` is absent. `NOTIFY_URL` still contains a legacy
ngrok URL, but the client gives the dedicated `NOTIFICATIONS_BASE_URL` higher
priority. `USE_FASTAPI`, `PROCESS_URL`, and `PUSH_URL` are not consumed by the
current Flutter endpoint resolver.

The production template also contains a parameter named `OPENAI_API_KEY`.
Remote Config is client-readable and must not contain backend secrets. Its
value was deliberately not displayed during this review. Confirm whether it is
populated; if so, rotate the credential, remove the parameter, and keep the new
credential only in the Oracle container secret environment.

The public Oracle checks returned HTTP 200 from `/health` and `/ready`.
OpenAPI exposed upload, assets, and notification routes but did not expose the
new admin role route at review time. This proves the service is reachable and
that the local role change is not deployed; it does not identify the running
Git commit or Docker image.

## Evidence required from Oracle

Capture these read-only values before deployment:

- `docker inspect kunivai-api` image ID/digest, creation time, environment key
  names (redact values), restart policy, mounts, and exposed ports.
- `docker image inspect <image>` labels and RepoDigests. Add a build-time
  `org.opencontainers.image.revision` label in a later release if it is absent.
- The active Caddy configuration or `caddy adapt` output for
  `api.kunivai.online`, including any path-specific routing.
- The container log start banner and results of `/health`, `/ready`, and
  `/openapi.json` through Caddy and directly on the container network.

## Staging order

No staging environment or staging Remote Config template was found in the two
repositories. Create one before production, or explicitly record that the
release is using a controlled production canary.

1. Build an immutable backend image labeled with the reviewed Git commit.
2. Start it under a new container name/port with production-equivalent Firebase
   credentials but no public traffic; run health, readiness, ordinary-user,
   trusted-admin, demotion, retry, upload/assets, and notification checks.
3. Route a staging hostname or a restricted Caddy path to that container.
4. Apply the reviewed Firestore rules to the staging Firebase project and run
   the emulator acceptance suite plus browser smoke tests.
5. Follow `admin_role_migration.md`, remove `BOOTSTRAP_ADMIN_UIDS`, then verify
   the environment no longer contains it.
6. Update production Remote Config only after the Oracle route set is verified.
   Current endpoint values already target Oracle, so no endpoint migration is
   expected for this phase.

## Rollback without restoring legacy authority

Do not roll the privileged paths back to an image that authorizes from
`users.role`. If the first hardened deployment fails and there is no earlier
hardened image, configure Caddy to return maintenance/503 for `/upload`,
`/assets*`, `/admin/users/*`, `/notifications/announcements` writes,
`/notifications/app-update`, and notification statistics before restoring the
old container for non-admin traffic. Keep the custom-claim Firestore rules.

Revert Flutter Hosting or Remote Config independently if needed. After a
successful hardened deployment, retain its image digest as the minimum secure
rollback target for later releases. Never restore owner write access to `role`
and never bulk-create claims from existing Firestore role values.
