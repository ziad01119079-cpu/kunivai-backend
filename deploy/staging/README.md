# Kuniv isolated staging preparation

These files contain no credentials and do not deploy anything. The Remote
Config file is a merge fragment: merge it into an exported staging template;
do not publish it as a replacement for a complete template.

## Backend image and Oracle container

From a clean backend worktree at `3f803c5e50f58bd7f9e50561b20f552451f4a11a`:

1. Build `kunivai-api:test-3f803c5` with the OCI revision label documented in
   `docs/oracle_firebase_release_checklist.md`.
2. Put a populated copy of `backend.env.example` and the staging Firebase
   service-account JSON outside the repository with operator-only permissions.
3. Set `KUNIV_STAGING_ENV_FILE` and
   `KUNIV_STAGING_FIREBASE_CREDENTIALS`, then run
   `docker compose -f deploy/staging/compose.yaml config` and inspect the
   output without recording secret values.
4. Start the container, confirm the healthcheck, and load the restricted Caddy
   example only after setting `KUNIV_STAGING_ALLOWED_CIDR`.

## Firebase and Flutter Web

1. Create a non-production Firebase project and Web app. Enable Auth,
   Firestore, Hosting, and Remote Config; create two reviewed test-admin UIDs
   and one ordinary user.
2. Generate staging-only Flutter Firebase options. Do not deploy the existing
   `firebase_options.dart`, which targets `kuwaitchat-ai-project`, to staging.
3. Merge `remote-config.fragment.json` into the staging Remote Config export.
   Confirm the resulting template contains no `OPENAI_API_KEY` parameter.
4. Deploy the reviewed Flutter `firestore.rules` to the staging project, build
   Flutter Web at `2981dcaab75c4dd28830fad0f5d634e5168b6da1`, and deploy only
   to the staging Hosting target.
5. Verify ordinary profile updates, refreshed-admin access, stale-token denial,
   same-key role retry, competing-operation conflict, knowledge files, and
   notification administration before any production change.
