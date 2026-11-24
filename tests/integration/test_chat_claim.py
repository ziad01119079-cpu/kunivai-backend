"""Run only with a disposable loopback Firestore emulator."""

import asyncio
import os
from uuid import uuid4

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1.async_client import AsyncClient

from app import chat_repository


def test_firestore_claim_is_unique_and_completed_answer_immutable(monkeypatch):
    target = os.getenv("FIRESTORE_EMULATOR_HOST", "")
    if not target.startswith("127.0.0.1:") or not target[10:].isdigit():
        pytest.skip("Requires a loopback Firestore emulator")

    async def run():
        db = AsyncClient(project="demo-kuniv-certification", credentials=AnonymousCredentials())
        monkeypatch.setattr(chat_repository, "_db", lambda: db)
        uid, session, key = (uuid4().hex for _ in range(3))
        tokens = ("a", "b")
        try:
            results = await asyncio.gather(*(
                chat_repository.claim_generation(uid, session, key, token, "digest")
                for token in tokens
            ))
            assert sorted(results) == ["claimed", "existing"]
            winner = tokens[results.index("claimed")]
            other = tokens[results.index("existing")]
            assert not await chat_repository.save_assistant_message(
                uid, session, "stale", [], key, generation_fence=9999, generation_claim=other,
            )
            assert await chat_repository.save_assistant_message(
                uid, session, "current", [], key, generation_fence=1, generation_claim=winner,
            )
            assert not await chat_repository.save_assistant_message(
                uid, session, "repeated", [], key, generation_fence=1, generation_claim=winner,
            )
            assert await chat_repository.claim_generation(uid, session, key, "new", "digest") == "existing"
            assert await chat_repository.claim_generation(uid, session, key, "new", "different") == "conflict"
            answer = await db.document(f"users/{uid}/chats/{session}/messages/{key}-assistant").get()
            assert answer.to_dict()["content"] == "current"
        finally:
            db.close()

    asyncio.run(run())
