"""Replay revocation guard for local RequestCoalescer and distributed Redis SSE streams (COMM-004 Finding 1).

Ensures that buffered or reconnected SSE events carrying Community Knowledge
are revalidated against authoritative revocation state before emission:
- Non-Community / Official-only events pass through unchanged with zero overhead.
- Revoked or unknown-provenance Community chunks are suppressed/tombstoned.
- On mixed answers, independently grounded Official chunks & citations are retained
  while revoked Community chunks & citations are stripped.
- Fails closed on any revocation lookup error.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)


async def verify_records_still_published(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lazy wrapper avoiding circular import between coordination_runtime and community_service."""
    from .community_service import verify_records_still_published as _verify

    return await _verify(records)

RevalidateFn = Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]

REPLAY_REVOKED_NOTICE_AR = (
    "\n\n### تجارب وآراء الطلبة (غير متاحة)\n\n"
    "تم حجب قسم آراء الطلبة في إعادة البث لأن أحد مصادره سُحب لاحقاً، "
    "بينما تبقى المعلومات الرسمية أعلاه مستقلة ومعتمدة."
)


def _split_sse_event(event: str) -> tuple[str, dict[str, Any] | None]:
    """Extract optional SSE prefix lines (e.g. `id: ...`) and parsed JSON data payload."""
    prefix_lines: list[str] = []
    payload: dict[str, Any] | None = None
    for line in event.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except ValueError:
                return event, None
        elif line.strip():
            prefix_lines.append(line)
    prefix = ("\n".join(prefix_lines) + "\n") if prefix_lines else ""
    return prefix, payload


def _encode_sse_event(prefix: str, payload: dict[str, Any]) -> str:
    return f"{prefix}data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def extract_community_refs_from_payload(payload: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Identify whether an SSE payload contains Community Knowledge and extract its version refs."""
    is_community = False
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    if payload.get("source_kind") == "community_opinion":
        is_community = True

    raw_versions = payload.get("community_source_versions")
    if isinstance(raw_versions, list) and len(raw_versions) > 0:
        is_community = True
        for item in raw_versions:
            if isinstance(item, dict) and item.get("community_id"):
                cid = str(item["community_id"])
                ver = int(item.get("approval_version") or 0)
                key = (cid, ver)
                if key not in seen:
                    seen.add(key)
                    refs.append({"community_id": cid, "approval_version": ver})

    citations = payload.get("citations")
    if isinstance(citations, list):
        for cit in citations:
            if isinstance(cit, dict) and cit.get("source_kind") == "community_opinion":
                is_community = True
                if cit.get("community_id"):
                    cid = str(cit["community_id"])
                    ver = int(cit.get("approval_version") or 0)
                    key = (cid, ver)
                    if key not in seen:
                        seen.add(key)
                        refs.append({"community_id": cid, "approval_version": ver})

    return is_community, refs


async def _refs_still_valid(
    refs: list[dict[str, Any]],
    revalidate_fn: RevalidateFn,
) -> tuple[bool, set[tuple[str, int]]]:
    """Return (all_valid, surviving_keys). Fails closed on any lookup exception."""
    if not refs:
        return False, set()
    try:
        surviving = await revalidate_fn(refs)
    except Exception as exc:
        logger.warning("Replay revocation check failed closed (%s)", type(exc).__name__)
        return False, set()
    surviving_keys = {
        (str(item.get("community_id") or ""), int(item.get("approval_version") or 0))
        for item in surviving
        if isinstance(item, dict) and item.get("community_id")
    }
    expected_keys = {(r["community_id"], int(r["approval_version"])) for r in refs}
    return surviving_keys == expected_keys, surviving_keys


async def filter_sse_event_for_replay(
    event: str,
    *,
    revalidate_fn: RevalidateFn | None = None,
) -> tuple[str | None, bool, bool]:
    """Revalidate a single SSE event before replay/reconnected emission.

    Returns `(filtered_event_or_None, was_revoked, is_non_community_chunk)`.
    """
    prefix, payload = _split_sse_event(event)
    if payload is None:
        return event, False, False

    ev_type = payload.get("type")
    is_community, refs = extract_community_refs_from_payload(payload)
    if not is_community:
        return event, False, (ev_type == "chunk")

    checker = revalidate_fn or verify_records_still_published
    all_valid, surviving_keys = await _refs_still_valid(refs, checker)
    if all_valid:
        return event, False, False

    # Fail closed on revoked or unknown-provenance Community event.
    if ev_type == "chunk":
        return None, True, False

    if ev_type == "done":
        clean_payload = dict(payload)
        raw_cits = clean_payload.get("citations")
        if isinstance(raw_cits, list):
            clean_cits = []
            for cit in raw_cits:
                if isinstance(cit, dict) and cit.get("source_kind") == "community_opinion":
                    key = (str(cit.get("community_id") or ""), int(cit.get("approval_version") or 0))
                    if key in surviving_keys:
                        clean_cits.append(cit)
                else:
                    clean_cits.append(cit)
            clean_payload["citations"] = clean_cits
        remaining_versions = [
            {"community_id": cid, "approval_version": ver}
            for cid, ver in sorted(surviving_keys)
        ]
        if remaining_versions:
            clean_payload["community_source_versions"] = remaining_versions
        else:
            clean_payload.pop("community_source_versions", None)
        clean_payload["community_replay_redacted"] = True
        return _encode_sse_event(prefix, clean_payload), True, False

    return None, True, False


def community_revoked_replay_error() -> str:
    """Safe tombstone SSE error emitted when a pure-Community replay was retracted."""
    return "data: " + json.dumps(
        {
            "type": "error",
            "code": "COMMUNITY_SOURCE_REVOKED",
            "retryable": False,
            "message": "تم سحب أحد مصادر آراء الطلبة في هذه الإجابة ولم تعد متاحة لإعادة البث.",
        },
        ensure_ascii=False,
    ) + "\n\n"


async def filter_replay_sequence(
    events: Iterable[str],
    *,
    revalidate_fn: RevalidateFn | None = None,
) -> list[str]:
    """Filter an entire buffered SSE replay sequence (used by RequestCoalescer)."""
    filtered: list[str] = []
    any_revoked = False
    had_non_community_chunk = False
    had_any_surviving_chunk = False

    for event in events:
        out, revoked, is_official_chunk = await filter_sse_event_for_replay(
            event,
            revalidate_fn=revalidate_fn,
        )
        if revoked:
            any_revoked = True
        if is_official_chunk:
            had_non_community_chunk = True
        if out is not None:
            _, parsed = _split_sse_event(out)
            if parsed and parsed.get("type") == "chunk":
                had_any_surviving_chunk = True
            filtered.append(out)

    if not any_revoked:
        return filtered

    # Mixed answer: Official chunks survived -> retain Official answer + append safe redacted notice before done.
    if had_non_community_chunk:
        notice = "data: " + json.dumps({"type": "chunk", "content": REPLAY_REVOKED_NOTICE_AR}, ensure_ascii=False) + "\n\n"
        if filtered:
            _, last_parsed = _split_sse_event(filtered[-1])
            if last_parsed and last_parsed.get("type") == "done":
                return filtered[:-1] + [notice, filtered[-1]]
        return filtered + [notice]

    # Pure Community replay where revoked content was stripped -> emit safe tombstone error only.
    if not had_any_surviving_chunk:
        return [community_revoked_replay_error()]

    return filtered
