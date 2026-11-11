import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from google.api_core.exceptions import AlreadyExists

from app.auth import verify_admin, verify_token
from app.routers.admin_users import router
from app.services.admin_user_service import RoleOperationConflict, change_user_role
from main import app


class _Reference:
    def __init__(self):
        self.created = []
        self.updated = []
        self.data = None

    async def get(self):
        return SimpleNamespace(
            exists=self.data is not None,
            to_dict=lambda: dict(self.data or {}),
        )

    async def create(self, data):
        if self.data is not None:
            raise AlreadyExists("Document already exists")
        self.created.append(data)
        self.data = dict(data)

    async def update(self, data):
        self.updated.append(data)
        if self.data is None:
            raise RuntimeError("Document does not exist")
        self.data.update(data)


class _Collection:
    def __init__(self):
        self.references = {}

    def document(self, document_id):
        return self.references.setdefault(document_id, _Reference())


class _Batch:
    def __init__(self, db):
        self.db = db
        self.creates = []
        self.sets = []
        self.updates = []
        self.deletes = []
        self.committed = False

    def create(self, reference, data):
        self.creates.append((reference, data))

    def set(self, reference, data, merge=False):
        self.sets.append((reference, data, merge))

    def update(self, reference, data):
        self.updates.append((reference, data))

    def delete(self, reference):
        self.deletes.append(reference)

    async def commit(self):
        self.db.commit_count += 1
        if self.db.fail_commit_numbers and self.db.commit_count in self.db.fail_commit_numbers:
            raise RuntimeError("simulated Firestore commit failure")
        for reference, data in self.creates:
            await reference.create(data)
        for reference, data, merge in self.sets:
            if merge and reference.data is not None:
                reference.data.update(data)
            else:
                reference.data = dict(data)
        for reference, data in self.updates:
            await reference.update(data)
        for reference in self.deletes:
            reference.data = None
        self.committed = True


class _Db:
    def __init__(self, *, fail_commit_numbers=None):
        self.collections = {}
        self.last_batch = None
        self.commit_count = 0
        self.fail_commit_numbers = set(fail_commit_numbers or [])

    def collection(self, name):
        return self.collections.setdefault(name, _Collection())

    def batch(self):
        self.last_batch = _Batch(self)
        return self.last_batch


class AdminAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_claim_is_the_normal_trust_source(self):
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="token"
        )
        with (
            patch("app.auth._decode_token", AsyncMock(return_value={"uid": "admin-1", "admin": True})),
            patch("app.auth._admin_access_is_blocked", AsyncMock(return_value=False)),
            patch.dict(os.environ, {"BOOTSTRAP_ADMIN_UIDS": ""}, clear=False),
        ):
            self.assertEqual(await verify_admin(credentials), "admin-1")

    async def test_firestore_role_is_not_sufficient_without_claim(self):
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="token"
        )
        with (
            patch("app.auth._decode_token", AsyncMock(return_value={"uid": "legacy-admin"})),
            patch("app.auth._admin_access_is_blocked", AsyncMock(return_value=False)),
            patch.dict(os.environ, {"BOOTSTRAP_ADMIN_UIDS": ""}, clear=False),
        ):
            with self.assertRaises(HTTPException) as raised:
                await verify_admin(credentials)
        self.assertEqual(raised.exception.status_code, 403)

    async def test_deployment_bootstrap_allowlist_is_explicit(self):
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="token"
        )
        with (
            patch("app.auth._decode_token", AsyncMock(return_value={"uid": "reviewed-root"})),
            patch("app.auth._admin_access_is_blocked", AsyncMock(return_value=False)),
            patch.dict(
                os.environ,
                {"BOOTSTRAP_ADMIN_UIDS": "reviewed-root, another-root"},
                clear=False,
            ),
        ):
            self.assertEqual(await verify_admin(credentials), "reviewed-root")

    async def test_token_issued_before_demotion_is_denied_by_access_block(self):
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="old-admin-token"
        )
        decode = AsyncMock(return_value={"uid": "demoted-admin", "admin": True})
        with (
            patch("app.auth._decode_token", decode),
            patch("app.auth._admin_access_is_blocked", AsyncMock(return_value=True)),
            patch.dict(os.environ, {"BOOTSTRAP_ADMIN_UIDS": ""}, clear=False),
        ):
            with self.assertRaises(HTTPException) as raised:
                await verify_admin(credentials)
        self.assertEqual(raised.exception.status_code, 403)
        decode.assert_awaited_once_with("old-admin-token", check_revoked=True)

    async def test_firebase_revoked_token_is_rejected_as_unauthorized(self):
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="revoked-token"
        )
        with patch(
            "app.auth._decode_token",
            AsyncMock(side_effect=RuntimeError("revoked token")),
        ):
            with self.assertRaises(HTTPException) as raised:
                await verify_admin(credentials)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("revoked token", raised.exception.detail)

    async def test_demoted_identity_still_passes_ordinary_authentication(self):
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="ordinary-token-after-demotion"
        )
        with patch(
            "app.auth._decode_token",
            AsyncMock(return_value={"uid": "demoted-user", "admin": False}),
        ) as decode:
            self.assertEqual(await verify_token(credentials), "demoted-user")
        decode.assert_awaited_once_with("ordinary-token-after-demotion")


class AdminRoleServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_promotion_sets_claim_projection_and_audit(self):
        db = _Db()
        target = SimpleNamespace(custom_claims={"existing": "preserved"})
        with (
            patch("app.services.admin_user_service.auth.get_user", return_value=target),
            patch("app.services.admin_user_service.auth.set_custom_user_claims") as set_claims,
            patch("app.services.admin_user_service.auth.revoke_refresh_tokens") as revoke,
        ):
            result = await change_user_role(
                actor_uid="admin-1",
                target_uid="user-2",
                role="admin",
                reason="Approved against the staff roster",
                operation_id="promotion-operation-0001",
                db=db,
            )

        set_claims.assert_called_once_with(
            "user-2", {"existing": "preserved", "admin": True}
        )
        revoke.assert_called_once_with("user-2")
        self.assertEqual(result["previousRole"], "user")
        self.assertEqual(result["role"], "admin")
        self.assertTrue(db.last_batch.committed)
        projection = db.last_batch.sets[0][1]
        self.assertEqual(projection["role"], "admin")
        audit_collection = db.collections["admin_role_audit"]
        audit = next(iter(audit_collection.references.values()))
        self.assertEqual(audit.created[0]["status"], "requested")
        self.assertEqual(audit.created[0]["actorUid"], "admin-1")

    async def test_self_role_change_is_rejected_before_any_write(self):
        db = _Db()
        with self.assertRaises(ValueError):
            await change_user_role(
                actor_uid="same-user",
                target_uid="same-user",
                role="user",
                reason="Attempted self demotion",
                operation_id="self-demotion-0001",
                db=db,
            )
        self.assertEqual(db.collections, {})

    async def test_demotion_retry_finishes_revocation_without_duplicate_audit(self):
        db = _Db()
        claims = {"admin": True, "existing": "preserved"}

        def get_user(_uid):
            return SimpleNamespace(custom_claims=dict(claims))

        def set_claims(_uid, value):
            claims.clear()
            claims.update(value or {})

        revoke_attempts = 0

        def revoke(_uid):
            nonlocal revoke_attempts
            revoke_attempts += 1
            if revoke_attempts == 1:
                raise RuntimeError("temporary Auth outage")

        operation_id = "demotion-operation-0001"
        with (
            patch("app.services.admin_user_service.auth.get_user", side_effect=get_user),
            patch(
                "app.services.admin_user_service.auth.set_custom_user_claims",
                side_effect=set_claims,
            ) as set_custom_claims,
            patch(
                "app.services.admin_user_service.auth.revoke_refresh_tokens",
                side_effect=revoke,
            ),
        ):
            with self.assertRaises(RuntimeError):
                await change_user_role(
                    actor_uid="admin-1",
                    target_uid="admin-2",
                    role="user",
                    reason="Removed from the approved staff roster",
                    operation_id=operation_id,
                    db=db,
                )

            block = db.collections["admin_access_blocks"].document("admin-2")
            self.assertTrue(block.data["blocked"])
            audit = db.collections["admin_role_audit"].document(operation_id)
            self.assertEqual(audit.data["status"], "revocation_failed")
            self.assertNotIn("admin", claims)

            old_credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials="issued-before-demotion"
            )
            with (
                patch(
                    "app.auth._decode_token",
                    AsyncMock(return_value={"uid": "admin-2", "admin": True}),
                ),
                patch("app.auth.firestore_async.client", return_value=db),
            ):
                with self.assertRaises(HTTPException) as old_token_rejected:
                    await verify_admin(old_credentials)
            self.assertEqual(old_token_rejected.exception.status_code, 403)

            result = await change_user_role(
                actor_uid="admin-1",
                target_uid="admin-2",
                role="user",
                reason="Removed from the approved staff roster",
                operation_id=operation_id,
                db=db,
            )

        self.assertEqual(result["role"], "user")
        self.assertEqual(audit.data["status"], "applied")
        self.assertEqual(len(audit.created), 1)
        self.assertEqual(revoke_attempts, 2)
        self.assertTrue(
            all(not value or value.get("admin") is not True for _, value in set_custom_claims.call_args_list)
        )

    async def test_projection_failure_retries_same_audit(self):
        db = _Db(fail_commit_numbers={2})
        target = SimpleNamespace(custom_claims={})
        operation_id = "projection-retry-0001"
        with (
            patch("app.services.admin_user_service.auth.get_user", return_value=target),
            patch("app.services.admin_user_service.auth.set_custom_user_claims"),
            patch("app.services.admin_user_service.auth.revoke_refresh_tokens"),
        ):
            with self.assertRaises(RuntimeError):
                await change_user_role(
                    actor_uid="admin-1",
                    target_uid="user-3",
                    role="admin",
                    reason="Approved after independent owner review",
                    operation_id=operation_id,
                    db=db,
                )
            result = await change_user_role(
                actor_uid="admin-1",
                target_uid="user-3",
                role="admin",
                reason="Approved after independent owner review",
                operation_id=operation_id,
                db=db,
            )

        audit = db.collections["admin_role_audit"].document(operation_id)
        self.assertEqual(result["role"], "admin")
        self.assertEqual(len(audit.created), 1)
        self.assertEqual(audit.data["status"], "applied")

    async def test_old_promotion_retry_cannot_undo_newer_demotion(self):
        db = _Db()
        claims = {}

        def get_user(_uid):
            return SimpleNamespace(custom_claims=dict(claims))

        def set_claims(_uid, value):
            claims.clear()
            claims.update(value or {})

        with (
            patch("app.services.admin_user_service.auth.get_user", side_effect=get_user),
            patch(
                "app.services.admin_user_service.auth.set_custom_user_claims",
                side_effect=set_claims,
            ) as set_custom_claims,
            patch("app.services.admin_user_service.auth.revoke_refresh_tokens"),
        ):
            await change_user_role(
                actor_uid="admin-1",
                target_uid="user-4",
                role="admin",
                reason="Initial owner-approved promotion",
                operation_id="promotion-user-4-0001",
                db=db,
            )
            await change_user_role(
                actor_uid="admin-1",
                target_uid="user-4",
                role="user",
                reason="Later removal from the approved roster",
                operation_id="demotion-user-4-0002",
                db=db,
            )
            calls_before_old_retry = set_custom_claims.call_count
            with self.assertRaises(RoleOperationConflict):
                await change_user_role(
                    actor_uid="admin-1",
                    target_uid="user-4",
                    role="admin",
                    reason="Initial owner-approved promotion",
                    operation_id="promotion-user-4-0001",
                    db=db,
                )

        self.assertEqual(set_custom_claims.call_count, calls_before_old_retry)
        self.assertNotIn("admin", claims)
        self.assertEqual(db.collections["users"].document("user-4").data["role"], "user")
        self.assertTrue(
            db.collections["admin_access_blocks"].document("user-4").data["blocked"]
        )

    async def test_competing_operation_cannot_enter_while_uid_lock_is_held(self):
        db = _Db()
        claims = {"admin": True}
        revoke_attempts = 0

        def get_user(_uid):
            return SimpleNamespace(custom_claims=dict(claims))

        def set_claims(_uid, value):
            claims.clear()
            claims.update(value or {})

        def fail_first_revoke(_uid):
            nonlocal revoke_attempts
            revoke_attempts += 1
            if revoke_attempts == 1:
                raise RuntimeError("temporary failure keeps the UID lock")

        with (
            patch("app.services.admin_user_service.auth.get_user", side_effect=get_user),
            patch(
                "app.services.admin_user_service.auth.set_custom_user_claims",
                side_effect=set_claims,
            ),
            patch(
                "app.services.admin_user_service.auth.revoke_refresh_tokens",
                side_effect=fail_first_revoke,
            ),
        ):
            with self.assertRaises(RuntimeError):
                await change_user_role(
                    actor_uid="admin-1",
                    target_uid="admin-5",
                    role="user",
                    reason="Approved emergency demotion",
                    operation_id="demotion-admin-5-0001",
                    db=db,
                )

            with self.assertRaises(RoleOperationConflict):
                await change_user_role(
                    actor_uid="admin-1",
                    target_uid="admin-5",
                    role="admin",
                    reason="Conflicting promotion request",
                    operation_id="promotion-admin-5-0002",
                    db=db,
                )

            result = await change_user_role(
                actor_uid="admin-1",
                target_uid="admin-5",
                role="user",
                reason="Approved emergency demotion",
                operation_id="demotion-admin-5-0001",
                db=db,
            )

        self.assertEqual(result["role"], "user")
        self.assertNotIn("admin", claims)
        self.assertIsNone(
            db.collections["admin_role_locks"].document("admin-5").data
        )
        self.assertIsNone(
            db.collections["admin_role_audit"].document(
                "promotion-admin-5-0002"
            ).data
        )

    def test_admin_role_route_is_registered(self):
        self.assertIn("/admin/users/{target_uid}/role", {route.path for route in router.routes})

    def test_management_routes_depend_on_verified_admin(self):
        protected = {
            ("POST", "/upload"),
            ("GET", "/assets"),
            ("DELETE", "/assets/{file_id}"),
            ("PATCH", "/admin/users/{target_uid}/role"),
            ("POST", "/notifications/announcements"),
            ("POST", "/notifications/app-update"),
            ("GET", "/notifications/announcements/stats"),
        }
        dependencies = {
            (method, route.path): {dependency.call for dependency in route.dependant.dependencies}
            for route in app.routes
            for method in (route.methods or set())
            if (method, route.path) in protected
        }

        self.assertEqual(set(dependencies), protected)
        for route_dependencies in dependencies.values():
            self.assertIn(verify_admin, route_dependencies)


if __name__ == "__main__":
    unittest.main()
