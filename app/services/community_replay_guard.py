"""Replay revocation guard for local RequestCoalescer and distributed Redis SSE streams (H2, H3, COMM-006).

Ensures that buffered, live-queued, or reconnected SSE events carrying Community Knowledge
are strictly validated against authoritative revocation state before delivery:
- Rejects non-integer version strings and booleans (`True`/`False`) cleanly without raising.
- Supports explicit provenance checking (tombstones unknown/pre-upgrade chunks when required).
- Emits ONE atomic redacted terminal event (never duplicate SSE IDs for notice + done).
- Fails closed on any lookup exception.
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


def _parse_strict_version(raw: Any) -> int | None:
    """Accept ONLY real positive integers; reject bools (`True`/`False`) and non-int strings (H3)."""
    if type(raw) is int and raw >= 1:
        return raw
    return None


def extract_community_refs_from_payload(payload: dict[str, Any]) -> tuple[bool, list[dict[str, Any]], bool]:
    """Identify Community SSE payload, extract version refs, and flag malformed version metadata.

    Returns `(is_community, refs, malformed)`.
    """
    is_community = False
    malformed = False
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    if payload.get("source_kind") == "community_opinion":
        is_community = True

    if "community_source_versions" in payload:
        is_community = True
        raw_versions = payload.get("community_source_versions")
        if not isinstance(raw_versions, list) or len(raw_versions) == 0:
            malformed = True
        else:
            for item in raw_versions:
                if not isinstance(item, dict) or not isinstance(item.get("community_id"), str) or not item["community_id"].strip():
                    malformed = True
                    continue
                ver = _parse_strict_version(item.get("approval_version"))
                if ver is None:
                    malformed = True
                    continue
                cid = item["community_id"].strip()
                key = (cid, ver)
                if key not in seen:
                    seen.add(key)
                    refs.append({"community_id": cid, "approval_version": ver})

    citations = payload.get("citations")
    if isinstance(citations, list):
        for cit in citations:
            if isinstance(cit, dict) and cit.get("source_kind") == "community_opinion":
                is_community = True
                cid = cit.get("community_id")
                ver = _parse_strict_version(cit.get("approval_version"))
                if not isinstance(cid, str) or not cid.strip() or ver is None:
                    malformed = True
                    continue
                key = (cid.strip(), ver)
                if key not in seen:
                    seen.add(key)
                    refs.append({"community_id": cid.strip(), "approval_version": ver})

    if is_community and not refs:
        malformed = True

    return is_community, refs, malformed


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
    surviving_keys: set[tuple[str, int]] = set()
    for item in surviving:
        if isinstance(item, dict) and isinstance(item.get("community_id"), str):
            ver = _parse_strict_version(item.get("approval_version"))
            if ver is not None:
                surviving_keys.add((item["community_id"].strip(), ver))
    expected_keys = {(r["community_id"], r["approval_version"]) for r in refs}
    return surviving_keys == expected_keys, surviving_keys


async def filter_sse_event_for_replay(
    event: str,
    *,
    revalidate_fn: RevalidateFn | None = None,
    require_explicit_provenance: bool = False,
) -> tuple[str | None, bool, bool]:
    """Revalidate a single SSE event before replay/reconnected/live emission (H2, H3).

    Returns `(filtered_event_or_None, was_revoked, is_non_community_chunk)`.
    """
    prefix, payload = _split_sse_event(event)
    if payload is None:
        return event, False, False

    ev_type = payload.get("type")
    is_community, refs, malformed = extract_community_refs_from_payload(payload)

    # H3: Pre-upgrade/untagged chunk with unknown provenance fails closed when explicit provenance is required.
    if not is_community:
        if require_explicit_provenance and ev_type == "chunk" and not payload.get("source_kind"):
            return None, True, False
        return event, False, (ev_type == "chunk")

    if malformed:
        if ev_type == "chunk":
            return None, True, False
        if ev_type == "done":
            return _redact_done_event(prefix, payload, set()), True, False
        return None, True, False

    checker = revalidate_fn or verify_records_still_published
    all_valid, surviving_keys = await _refs_still_valid(refs, checker)
    if all_valid:
        return event, False, False

    if ev_type == "chunk":
        return None, True, False

    if ev_type == "done":
        return _redact_done_event(prefix, payload, surviving_keys), True, False

    return None, True, False


def _redact_done_event(prefix: str, payload: dict[str, Any], surviving_keys: set[tuple[str, int]]) -> str:
    """Build ONE atomic redacted terminal `done` event retaining Official citations only (H3)."""
    clean_payload = dict(payload)
    raw_cits = clean_payload.get("citations")
    if isinstance(raw_cits, list):
        clean_cits = []
        for cit in raw_cits:
            if isinstance(cit, dict) and cit.get("source_kind") == "community_opinion":
                ver = _parse_strict_version(cit.get("approval_version"))
                key = (str(cit.get("community_id") or "").strip(), ver or 0)
                if ver is not None and key in surviving_keys:
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
    clean_payload["redaction_notice"] = REPLAY_REVOKED_NOTICE_AR
    return _encode_sse_event(prefix, clean_payload)


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
    require_explicit_provenance: bool = False,
) -> list[str]:
    """Filter an ordered SSE sequence, returning an atomic redacted terminal if revoked."""
    filtered: list[str] = []
    any_revoked = False
    had_non_community_chunk = False
    had_any_surviving_chunk = False

    for event in events:
        out, revoked, is_official_chunk = await filter_sse_event_for_replay(
            event,
            revalidate_fn=revalidate_fn,
            require_explicit_provenance=require_explicit_provenance,
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

    if had_non_community_chunk:
        return filtered

    if not had_any_surviving_chunk:
        return [community_revoked_replay_error()]

    return filtered
