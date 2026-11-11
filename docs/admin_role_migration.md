# Administrator trust and migration runbook

## Authorization source of truth

Privileged requests are authorized by the Firebase Auth custom claim
`admin: true`. The Firestore field `users/{uid}.role` is only a UI projection.
It must not be used to grant a claim or to seed a permanent administrator.

`BOOTSTRAP_ADMIN_UIDS` is a deployment-controlled, temporary break-glass
allowlist. It is intended only to establish the first reviewed custom-claim
administrators and should be removed after the bootstrap is verified.

## Review before migration

1. Export the existing Firestore documents whose `role` is `admin` as a list
   of candidates only. Do not change Auth claims yet.
2. Have the project owner supply an independent, approved staff roster.
3. For every candidate, match the Firebase Auth UID and verified email/provider
   to that roster. Review creation time, recent sign-in information and disabled
   state in Firebase Auth. Investigate shared, stale or unexpected accounts.
4. Record an owner approval and a second reviewer for each UID. Exclude every
   UID that cannot be independently verified; the old Firestore value is not
   evidence of authority.
5. Keep the candidate export and approval record outside the app database with
   access limited to the project owners.

This review requires access to the production Firebase Auth tenant, production
Firestore and an owner-approved staff roster. The repository alone cannot
identify which current people should remain administrators.

## Bootstrap sequence

1. The owner approves at least two exact Firebase Auth UIDs, `A` and `B`, from
   the independently reviewed roster. Email or a Firestore `role` value alone
   is not sufficient.
2. Put only `A` in `BOOTSTRAP_ADMIN_UIDS` in the Oracle container environment,
   recreate `kunivai-api`, and verify that unlisted users receive HTTP 403.
3. `A` calls `PATCH /admin/users/B/role` with a meaningful `reason` and a unique
   `Idempotency-Key`. The same key must be reused if a 503 or uncertain network
   result is retried.
4. `B` signs in again or forces an ID-token refresh, verifies the `admin: true`
   claim, opens the admin dashboard, and exercises one read-only admin action.
5. `B` promotes `A` through the audited endpoint. `A` refreshes its token and
   verifies the same checks. Self-promotion and self-demotion are rejected.
6. Remove `BOOTSTRAP_ADMIN_UIDS`, recreate the container, and verify `A`, `B`
   and one ordinary account again. If there is only one legitimate
   administrator, set that first claim through a controlled Firebase Admin SDK
   operation with an external approval record instead of weakening the API.
7. Reconcile `admin_role_audit` entries whose status is
   `claim_applied_projection_failed`; the Auth claim remains authoritative.

Role changes preserve unrelated custom claims, revoke the target user's refresh
tokens, update the Firestore projection, and write an audit record. A promoted
user must refresh their ID token before Firestore rules recognize the new claim.

Demotion writes `admin_access_blocks/{uid}` before removing the claim. This
deny record overrides an already-issued admin token while Auth revocation is in
progress or being retried. Promotion removes the block only after the claim and
token revocation succeed. Audit document IDs are the caller's idempotency keys,
so a retry resumes the same operation instead of creating another audit entry.

Role changes are serialized per UID with `admin_role_locks/{uid}`. A competing
operation receives HTTP 409 and must not start with another key until the owner
is completed using its original key. `admin_role_state/{uid}` records the
latest intent, so replaying an older promotion after a newer demotion is
rejected as superseded. Locks and state are backend-only and are not readable
or writable by Firebase clients.

### Resume an interrupted locked operation

Do not delete `admin_role_locks/{uid}` manually. The lock's `operationId` is the
original `Idempotency-Key` and also the document ID in `admin_role_audit`.

1. Using Firebase Console or an Admin SDK session, read the lock for the target
   UID and record only `operationId`, `actorUid`, and `requestedRole`.
2. Open `admin_role_audit/{operationId}` and verify that its target, actor,
   requested role, and reason match the approved change. Review its status to
   identify the last completed checkpoint.
3. Re-send the exact original `PATCH /admin/users/{uid}/role` body with
   `Idempotency-Key: <operationId>` while authenticated as the approved actor.
   The backend resumes the same audit and removes the lock only in its final
   atomic batch.
4. If the API returns 409, another operation owns the UID. Recover that owner's
   key from the current lock and resume it first; do not invent a replacement
   key and do not clear the lock.
5. Verify the Auth claim, token revocation, `users.role`, access block, latest
   role state, audit status `applied`, and absence of the lock.

If the approved actor is no longer an administrator, restore that actor's
authority through the controlled bootstrap/Admin SDK procedure with an external
approval record, then resume the original key. The current implementation
intentionally rejects a different actor reusing the key; supporting ownership
transfer requires a separately reviewed code change, not manual lock deletion.

## Production verification

- Confirm the deployed backend revision and environment variables.
- Confirm the deployed Firestore rules require `admin == true` and the absence
  of `admin_access_blocks/{uid}` for administrative access.
- Exercise profile updates with an ordinary user and prove that changing
  `role`, `roleUpdatedAt`, or `roleUpdatedBy` is denied.
- Prove that role changes, knowledge upload/list/delete, announcement creation,
  app-update notification creation and notification statistics return HTTP 403
  for an ordinary token and succeed for a reviewed administrator token.
- Inspect Firebase Auth, the user projection, and the audit record after both a
  promotion and a demotion.
- Before demotion, retain an admin ID token `T0`. Immediately after the deny
  record is written, prove that `T0` cannot read another user's Firestore data
  or call `/assets`, while the same identity can still read and update its own
  allowed profile fields with an ordinary refreshed token.
- Send two role changes for the same UID concurrently. Expect one owner and one
  HTTP 409; finish the owner with its original idempotency key, then verify the
  Auth claim, deny record, user projection, latest role state, and single audit.
