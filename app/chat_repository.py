"""
app/chat_repository.py

Firestore read/write operations for chat messages and sessions.

Uses the async Firestore client to avoid blocking the event loop or thread pool exhaustion.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from firebase_admin import firestore_async
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from .config import get_config

logger = logging.getLogger(__name__)


def _db():
    """Return a Firestore async client."""
    return firestore_async.client()


# ---------------------------------------------------------------------------
# Message loading
# ---------------------------------------------------------------------------

async def load_recent_messages(
    uid: str,
    session_id: str,
    max_docs: int,
) -> list[dict]:
    """
    Load up to `max_docs` most-recent messages from Firestore,
    returned in descending order (newest first).
    """
    db = _db()
    
    query = (
        db.collection("users")
        .document(uid)
        .collection("chats")
        .document(session_id)
        .collection("messages")
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(max_docs)
    )
    
    snapshot = await query.get()
    return [doc.to_dict() for doc in snapshot]


# ---------------------------------------------------------------------------
# Message writing
# ---------------------------------------------------------------------------

async def save_user_message(
    uid: str,
    session_id: str,
    content: str,
    correlation_id: str | None = None,
) -> str:
    """
    Save the user's message to Firestore using a batch write.
    Returns the new document ID.
    """
    db = _db()
    batch = db.batch()
    
    messages_ref = (
        db.collection("users")
        .document(uid)
        .collection("chats")
        .document(session_id)
        .collection("messages")
    )
    # A client-generated UUID makes an ambiguous retry overwrite the same
    # logical user turn instead of creating a duplicate document.
    msg_ref = messages_ref.document(correlation_id) if correlation_id else messages_ref.document()
    
    msg_data = {
        "content": content,
        "isUser": True,
        "createdAt": firestore.SERVER_TIMESTAMP,
    }
    if correlation_id:
        msg_data["client_message_id"] = correlation_id
        
    batch.set(msg_ref, msg_data)

    session_ref = (
        db.collection("users")
        .document(uid)
        .collection("chats")
        .document(session_id)
    )
    batch.set(session_ref, {"lastMessageAt": firestore.SERVER_TIMESTAMP}, merge=True)
    # Denormalize the activity marker on the user document so the dashboard
    # can page recent users without scanning every chat subcollection.
    batch.set(
        db.collection("users").document(uid),
        {
            "lastMessageAt": firestore.SERVER_TIMESTAMP,
            "lastChatId": session_id,
        },
        merge=True,
    )

    await batch.commit()
    return msg_ref.id


async def claim_generation(uid: str, session_id: str, message_id: str, token: str, fingerprint: str) -> str:
    """Authorize at most one ambiguous generation for this durable message identity."""
    db = _db()
    session = db.collection("users").document(uid).collection("chats").document(session_id)
    claim = session.collection("generationClaims").document(message_id)
    answer = session.collection("messages").document(f"{message_id}-assistant")

    # Legacy writers must drain before cutover. Distributed writers can only
    # create an answer after winning this document's atomic create.
    current = await claim.get()
    if current.exists:
        return "existing" if current.to_dict().get("fingerprint") == fingerprint else "conflict"
    if (await answer.get()).exists:
        current = await claim.get()
        if current.exists and current.to_dict().get("fingerprint") != fingerprint:
            return "conflict"
        return "existing"
    try:
        await claim.create({"token": token, "fingerprint": fingerprint, "createdAt": firestore.SERVER_TIMESTAMP})
    except AlreadyExists:
        current = await claim.get()
        if current.exists and current.to_dict().get("fingerprint") != fingerprint:
            return "conflict"
        return "existing"
    return "claimed"


async def save_assistant_message(
    uid: str,
    session_id: str,
    content: str,
    citations: list[dict],
    correlation_id: str | None = None,
    generation_fence: int | None = None,
    generation_claim: str | None = None,
    provenance_kind: str | None = None,
) -> bool:
    """Save the assistant's response to Firestore using a batch write."""
    db = _db()
    batch = db.batch()
    
    messages_ref = (
        db.collection("users")
        .document(uid)
        .collection("chats")
        .document(session_id)
        .collection("messages")
    )
    assistant_id = f"{correlation_id}-assistant" if correlation_id else None
    msg_ref = messages_ref.document(assistant_id) if assistant_id else messages_ref.document()
    message_data = {
        "content": content,
        "isUser": False,
        "citations": citations,
        "createdAt": firestore.SERVER_TIMESTAMP,
    }
    if provenance_kind is not None:
        message_data["serverProvenanceKind"] = provenance_kind
    if generation_fence is not None:
        message_data["generationFence"] = generation_fence

    session_ref = (
        db.collection("users")
        .document(uid)
        .collection("chats")
        .document(session_id)
    )
    user_ref = db.collection("users").document(uid)
    if assistant_id and (generation_fence is not None or generation_claim is not None):
        transaction = db.transaction()

        @firestore.async_transactional
        async def commit_fenced(txn):
            existing = await msg_ref.get(transaction=txn)
            claim_ref = None
            if generation_claim is not None:
                claim_ref = session_ref.collection("generationClaims").document(correlation_id)
                claim = await claim_ref.get(transaction=txn)
                if not claim.exists or claim.to_dict().get("token") != generation_claim or existing.exists:
                    return False
            if existing.exists:
                previous = int(existing.to_dict().get("generationFence", 0) or 0)
                if previous > generation_fence:
                    logger.warning(
                        "Rejected stale assistant write fence=%s current=%s",
                        generation_fence,
                        previous,
                    )
                    return False
            txn.set(msg_ref, message_data)
            txn.set(session_ref, {"lastMessageAt": firestore.SERVER_TIMESTAMP}, merge=True)
            txn.set(user_ref, {
                "lastMessageAt": firestore.SERVER_TIMESTAMP,
                "lastChatId": session_id,
            }, merge=True)
            if claim_ref is not None:
                txn.update(claim_ref, {"completedAt": firestore.SERVER_TIMESTAMP})
            return True

        return await commit_fenced(transaction)

    batch.set(msg_ref, message_data)
    batch.set(session_ref, {"lastMessageAt": firestore.SERVER_TIMESTAMP}, merge=True)
    batch.set(user_ref, {
        "lastMessageAt": firestore.SERVER_TIMESTAMP,
        "lastChatId": session_id,
    }, merge=True)
    await batch.commit()
    return True
