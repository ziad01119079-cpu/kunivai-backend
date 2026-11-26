"""Offline regression tests for version-checked moderation transactions."""
import asyncio
import copy
from unittest.mock import AsyncMock

import pytest
from app import community_repository as repo


class Snapshot:
    def __init__(self, data):
        self.exists = data is not None
        self.data = copy.deepcopy(data)

    def to_dict(self):
        return copy.deepcopy(self.data)


class Doc:
    def __init__(self, db, group, key):
        self.db, self.group, self.key = db, group, key

    async def get(self, *, transaction=None):
        assert transaction is not None, "Read must be within the transaction"
        return Snapshot(self.db.docs.get((self.group, self.key)))


class Collection:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def document(self, key=None):
        if key is None:
            self.db.seq += 1
            key = f"audit-{self.db.seq}"
        return Doc(self.db, self.name, key)


class Transaction:
    def __init__(self, db):
        self.db = db
        self.pending = []

    def set(self, ref, value):
        self.pending.append(("set", ref, copy.deepcopy(value)))

    def create(self, ref, value):
        if ref.group == repo.AUDIT_COLLECTION and self.db.fail_audit:
            raise RuntimeError("audit write failure")
        self.pending.append(("create", ref, copy.deepcopy(value)))

    def commit(self):
        for kind, ref, value in self.pending:
            if kind == "create" and (ref.group, ref.key) in self.db.docs:
                raise RuntimeError("create conflict")
        for _, ref, value in self.pending:
            self.db.docs[(ref.group, ref.key)] = value


class FakeDB:
    def __init__(self, cid, initial, *, fail_audit=False):
        self.docs = {(repo.RECORDS_COLLECTION, cid): copy.deepcopy(initial)}
        self.fail_audit = fail_audit
        self.seq = 0
        self.lock = asyncio.Lock()

    def collection(self, group):
        return Collection(self, group)

    def transaction(self):
        return Transaction(self)


def backend(monkeypatch, db):
    monkeypatch.setattr(repo.firestore_async, "client", lambda: db)
    monkeypatch.setattr(repo, "_publish_distributed_revocation", AsyncMock())

    def decorator(fn):
        async def runner(tx):
            async with db.lock:
                result = await fn(tx)
                tx.commit()
                return result
        return runner

    monkeypatch.setattr(repo.firestore, "async_transactional", decorator)


def rec(cid, ver, status, approved=0):
    return {"community_id": cid, "record_version": ver,
            "moderation_status": status, "approval_version": approved}


def aud(cid, ver, before, after, allow=False):
    return {"community_id": cid, "previous_version": ver,
            "new_version": ver + 1, "previous_status": before,
            "new_status": after, "reapprove_after_retraction": allow}


def audit_count(db):
    return sum(k[0] == repo.AUDIT_COLLECTION for k in db.docs)


def test_two_simultaneous_approvals_one_wins(monkeypatch):
    cid, old_status, new_status = "CM-RACE", "NEEDS_MODERATION_REVIEW", "APPROVED_FOR_COMMUNITY_PUBLICATION"
    db = FakeDB(cid, rec(cid, 1, old_status))
    backend(monkeypatch, db)

    async def go():
        async def update():
            return await repo.transition_community_record_atomic(
                cid, expected_version=1, new_record=rec(cid, 2, new_status, 1),
                audit_entry=aud(cid, 1, old_status, new_status))
        return await asyncio.gather(update(), update(), return_exceptions=True)

    values = asyncio.run(go())
    assert sum(isinstance(v, dict) for v in values) == 1
    assert sum(isinstance(v, repo.CommunityConcurrencyError) for v in values) == 1
    assert audit_count(db) == 1
    assert db.docs[(repo.RECORDS_COLLECTION, cid)]["record_version"] == 2


def test_retract_marker_atomic_and_stale_approval_rejected(monkeypatch):
    cid, status = "CM-RETRACT", "APPROVED_FOR_COMMUNITY_PUBLICATION"
    db = FakeDB(cid, rec(cid, 2, status, 1))
    backend(monkeypatch, db)
    marker = {"community_id": cid, "revoked_approval_version": 1}

    async def go():
        await repo.transition_community_record_atomic(
            cid, expected_version=2, new_record=rec(cid, 3, "RETRACTED", 1),
            audit_entry=aud(cid, 2, status, "RETRACTED"),
            retraction_marker=marker)
        with pytest.raises(repo.CommunityConcurrencyError):
            await repo.transition_community_record_atomic(
                cid, expected_version=2, new_record=rec(cid, 3, status, 2),
                audit_entry=aud(cid, 2, status, status))
    asyncio.run(go())
    assert db.docs[(repo.REVOCATIONS_COLLECTION, cid)] == marker
    assert db.docs[(repo.RECORDS_COLLECTION, cid)]["moderation_status"] == "RETRACTED"
    assert audit_count(db) == 1


def test_failed_audit_rolls_back_everything(monkeypatch):
    cid, status = "CM-AUDIT", "APPROVED_FOR_COMMUNITY_PUBLICATION"
    initial = rec(cid, 1, status, 1)
    db = FakeDB(cid, initial, fail_audit=True)
    backend(monkeypatch, db)
    with pytest.raises(RuntimeError, match="audit write failure"):
        asyncio.run(repo.transition_community_record_atomic(
            cid, expected_version=1, new_record=rec(cid, 2, "RETRACTED", 1),
            audit_entry=aud(cid, 1, status, "RETRACTED"),
            retraction_marker={"community_id": cid, "revoked_approval_version": 1}))
    assert db.docs[(repo.RECORDS_COLLECTION, cid)] == initial
    assert (repo.REVOCATIONS_COLLECTION, cid) not in db.docs
    assert audit_count(db) == 0


def test_reapproval_requires_explicit_audit_and_new_version(monkeypatch):
    cid = "CM-REAPPROVE"
    db = FakeDB(cid, rec(cid, 2, "RETRACTED", 1))
    backend(monkeypatch, db)

    async def go():
        before = "RETRACTED"
        after = "APPROVED_FOR_COMMUNITY_PUBLICATION"
        with pytest.raises(repo.CommunityConcurrencyError):
            await repo.transition_community_record_atomic(
                cid, expected_version=2, new_record=rec(cid, 3, after, 2),
                audit_entry=aud(cid, 2, before, after))
        with pytest.raises(repo.CommunityConcurrencyError):
            await repo.transition_community_record_atomic(
                cid, expected_version=2, new_record=rec(cid, 3, after, 1),
                audit_entry=aud(cid, 2, before, after, allow=True))
        return await repo.transition_community_record_atomic(
            cid, expected_version=2, new_record=rec(cid, 3, after, 2),
            audit_entry=aud(cid, 2, before, after, allow=True))

    assert asyncio.run(go())["record_version"] == 3
    assert audit_count(db) == 1

@pytest.mark.parametrize("new_version", [0, 1, 3, True, "2", None])
def test_approval_version_cannot_jump_or_change_type(monkeypatch, new_version):
    cid = "CM-STRICT-APPROVAL"
    old = rec(cid, 2, "NEEDS_MODERATION_REVIEW", 1)
    db = FakeDB(cid, old)
    backend(monkeypatch, db)
    new_status = "APPROVED_FOR_COMMUNITY_PUBLICATION"
    with pytest.raises(repo.CommunityConcurrencyError):
        asyncio.run(repo.transition_community_record_atomic(
            cid, expected_version=2,
            new_record=rec(cid, 3, new_status, new_version),
            audit_entry=aud(cid, 2, old["moderation_status"], new_status),
        ))
    assert db.docs[(repo.RECORDS_COLLECTION, cid)] == old
    assert audit_count(db) == 0


@pytest.mark.parametrize("bad_approval", [0, 2, -1, True, "1", None])
def test_comm004_finding5_non_approval_transition_rejects_altered_approval_version(monkeypatch, bad_approval):
    """Non-approval transitions (REJECTED / RETRACTED) must preserve approval_version unchanged."""
    cid = "CM-NON-APPROVAL-MONO"
    old = rec(cid, 2, "APPROVED_FOR_COMMUNITY_PUBLICATION", 1)
    db = FakeDB(cid, old)
    backend(monkeypatch, db)
    with pytest.raises(repo.CommunityConcurrencyError):
        asyncio.run(repo.transition_community_record_atomic(
            cid,
            expected_version=2,
            new_record=rec(cid, 3, "RETRACTED", bad_approval),
            audit_entry=aud(cid, 2, old["moderation_status"], "RETRACTED"),
            retraction_marker={"community_id": cid, "revoked_approval_version": 1},
        ))
    assert db.docs[(repo.RECORDS_COLLECTION, cid)] == old
    assert audit_count(db) == 0


def test_comm004_finding5_redis_postcommit_failure_does_not_fail_firestore_retraction(monkeypatch):
    """If Redis broadcast raises postcommit, Firestore retraction remains committed and authoritative."""
    cid = "CM-REDIS-FAIL"
    old = rec(cid, 2, "APPROVED_FOR_COMMUNITY_PUBLICATION", 1)
    db = FakeDB(cid, old)
    backend(monkeypatch, db)
    # Restore real _publish_distributed_revocation with a failing Redis client.
    monkeypatch.setattr(repo, "distributed_enabled", lambda: True)

    class _FailingRedisClient:
        async def hset(self, *_args, **_kwargs):
            raise RuntimeError("Redis down")

    fake_runtime = type("R", (), {"client": _FailingRedisClient(), "requests": type("Req", (), {"prefix": "kuniv"})()})()
    monkeypatch.setattr(repo, "get_redis_runtime", lambda: fake_runtime)
    monkeypatch.setattr(repo, "_publish_distributed_revocation", repo._publish_distributed_revocation)

    result = asyncio.run(repo.transition_community_record_atomic(
        cid,
        expected_version=2,
        new_record=rec(cid, 3, "RETRACTED", 1),
        audit_entry=aud(cid, 2, old["moderation_status"], "RETRACTED"),
        retraction_marker={"community_id": cid, "revoked_approval_version": 1},
    ))
    assert result["moderation_status"] == "RETRACTED"
    assert db.docs[(repo.REVOCATIONS_COLLECTION, cid)]["revoked_approval_version"] == 1
    assert audit_count(db) == 1


@pytest.mark.parametrize("order", ["approve_first", "retract_first"])
def test_comm004_finding5_concurrent_approve_vs_retract_both_orderings(monkeypatch, order):
    """Concurrent approve vs retract on same expected_version: exactly one wins, the other raises ConcurrencyError."""
    cid = f"CM-RACE-{order}"
    old = rec(cid, 2, "APPROVED_FOR_COMMUNITY_PUBLICATION", 1)
    db = FakeDB(cid, old)
    backend(monkeypatch, db)

    async def do_approve():
        return await repo.transition_community_record_atomic(
            cid,
            expected_version=2,
            new_record=rec(cid, 3, "APPROVED_FOR_COMMUNITY_PUBLICATION", 2),
            audit_entry=aud(cid, 2, old["moderation_status"], "APPROVED_FOR_COMMUNITY_PUBLICATION"),
        )

    async def do_retract():
        return await repo.transition_community_record_atomic(
            cid,
            expected_version=2,
            new_record=rec(cid, 3, "RETRACTED", 1),
            audit_entry=aud(cid, 2, old["moderation_status"], "RETRACTED"),
            retraction_marker={"community_id": cid, "revoked_approval_version": 1},
        )

    async def go():
        tasks = (do_approve(), do_retract()) if order == "approve_first" else (do_retract(), do_approve())
        return await asyncio.gather(*tasks, return_exceptions=True)

    results = asyncio.run(go())
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, repo.CommunityConcurrencyError) for r in results) == 1
    assert audit_count(db) == 1
