# Community Knowledge Publication Eligibility Report (Existing 18 Records)

**Source Dataset:** `F:\restored_files\knowledge_research\v1.3.1\community_candidate\community_knowledge.jsonl` (from `Kuniv_KB_Phase2_Readiness_v1.3.1.zip`)  
**Policy Engine:** `app/services/community_policy.py::evaluate_for_publication` (`evaluate_community_dataset`)  
**Date Evaluated:** 2026-11-25

## Executive Summary

| Metric | Count |
| --- | ---: |
| Total records evaluated (`CM-001`–`CM-018`) | **18** |
| Eligible for Community Publication | **0** |
| Blocked / Excluded (Fail-Closed) | **18** |

**Conclusion:** **0 of 18** existing community items pass publication eligibility. None was auto-promoted, and no URLs, publication dates, or independent student counts were fabricated. All 18 items remain strictly quarantined and excluded from Official Knowledge and production serving.

## Blocking Reason Breakdown

| Blocking Reason | Records Affected |
| --- | ---: |
| `NO_VALID_PUBLIC_POST_URL` | 18 |
| `NO_VALID_PUBLICATION_DATE` | 18 |
| `PROVENANCE_UNVERIFIED:UNVERIFIED` | 18 |
| `PRIVACY_REVIEW_NOT_PASSED` | 18 |
| `RIGHTS_REVIEW_NOT_PASSED` | 18 |
| `MISSING_ACCOUNTABLE_SIGNOFF` | 18 |
| `PERSON_REPUTATION_REVIEW_MISSING` | 9 |
| `MODERATION_NOT_APPROVED:NEEDS_MODERATION_REVIEW` | 8 |
| `MODERATION_NOT_APPROVED:EXCLUDED_MUST_USE_OFFICIAL_KB` | 5 |
| `FORBIDDEN_CONTENT_NATURE:UNOFFICIAL_CLAIM_ABOUT_OFFICIAL_REGULATION` | 5 |
| `MODERATION_NOT_APPROVED:BLOCKED_SENSITIVE_DEFAMATION_REVIEW` | 3 |
| `SENSITIVITY_OR_DEFAMATION_BLOCKED` | 3 |
| `MODERATION_NOT_APPROVED:NEEDS_OFFICIAL_VERIFICATION` | 2 |
| `FORBIDDEN_CONTENT_NATURE:FACTUAL_CLAIM_UNVERIFIED` | 2 |

## Per-Record Eligibility Table

| ID | Dedup Group | Category | Content Nature | Moderation Status | Eligible | Primary Blockers |
| --- | --- | --- | --- | --- | :---: | --- |
| `CM-001` | `DG-PROF-01` | `professors` | `PERSONAL_OPINION` | `BLOCKED_SENSITIVE_DEFAMATION_REVIEW` | No | Sensitive/defamation blocked; person reputation review missing; no URL/date/provenance/privacy/rights/signoff |
| `CM-002` | `DG-PROF-02` | `professors` | `PERSONAL_OPINION` | `NEEDS_MODERATION_REVIEW` | No | Moderation unapproved; person reputation review missing; no URL/date/provenance/privacy/rights/signoff |
| `CM-003` | `DG-PROF-03` | `professors` | `PERSONAL_OPINION` | `NEEDS_MODERATION_REVIEW` | No | Moderation unapproved; person reputation review missing; no URL/date/provenance/privacy/rights/signoff |
| `CM-004` | `DG-PROF-04` | `professors` | `PERSONAL_OPINION` | `BLOCKED_SENSITIVE_DEFAMATION_REVIEW` | No | Sensitive/defamation blocked; person reputation review missing; no URL/date/provenance/privacy/rights/signoff |
| `CM-005` | `DG-PROF-05` | `professors` | `PERSONAL_OPINION` | `NEEDS_MODERATION_REVIEW` | No | Moderation unapproved; person reputation review missing; no URL/date/provenance/privacy/rights/signoff |
| `CM-006` | `DG-PROF-06` | `professors` | `PERSONAL_OPINION` | `BLOCKED_SENSITIVE_DEFAMATION_REVIEW` | No | Sensitive/defamation blocked; person reputation review missing; no URL/date/provenance/privacy/rights/signoff |
| `CM-007` | `DG-PROF-07` | `professors` | `PERSONAL_OPINION` | `NEEDS_MODERATION_REVIEW` | No | Moderation unapproved; person reputation review missing; no URL/date/provenance/privacy/rights/signoff |
| `CM-008` | `DG-PROF-08` | `professors` | `PERSONAL_OPINION` | `NEEDS_MODERATION_REVIEW` | No | Moderation unapproved; person reputation review missing; no URL/date/provenance/privacy/rights/signoff |
| `CM-009` | `DG-COURSE-01` | `courses` | `PERSONAL_OPINION` | `NEEDS_MODERATION_REVIEW` | No | Moderation unapproved; person reputation review missing; no URL/date/provenance/privacy/rights/signoff |
| `CM-010` | `DG-REG-01` | `other` | `UNOFFICIAL_CLAIM_ABOUT_OFFICIAL_REGULATION` | `EXCLUDED_MUST_USE_OFFICIAL_KB` | No | Forbidden content nature (official regulation claim); no URL/date/provenance/reviews/signoff |
| `CM-011` | `DG-REG-02` | `courses` | `UNOFFICIAL_CLAIM_ABOUT_OFFICIAL_REGULATION` | `EXCLUDED_MUST_USE_OFFICIAL_KB` | No | Forbidden content nature (official regulation claim); no URL/date/provenance/reviews/signoff |
| `CM-012` | `DG-REG-03` | `campus_services` | `UNOFFICIAL_CLAIM_ABOUT_OFFICIAL_REGULATION` | `EXCLUDED_MUST_USE_OFFICIAL_KB` | No | Forbidden content nature (official regulation claim); no URL/date/provenance/reviews/signoff |
| `CM-013` | `DG-SERV-01` | `campus_services` | `FACTUAL_CLAIM_UNVERIFIED` | `NEEDS_OFFICIAL_VERIFICATION` | No | Forbidden content nature (unverified factual claim); no URL/date/provenance/reviews/signoff |
| `CM-014` | `DG-SERV-02` | `campus_services` | `STUDENT_EXPERIENCE_GUIDANCE` | `NEEDS_MODERATION_REVIEW` | No | Moderation unapproved; no URL/date/provenance/privacy/rights/signoff |
| `CM-015` | `DG-REG-04` | `other` | `UNOFFICIAL_CLAIM_ABOUT_OFFICIAL_REGULATION` | `EXCLUDED_MUST_USE_OFFICIAL_KB` | No | Forbidden content nature (official regulation claim); no URL/date/provenance/reviews/signoff |
| `CM-016` | `DG-REG-05` | `other` | `UNOFFICIAL_CLAIM_ABOUT_OFFICIAL_REGULATION` | `EXCLUDED_MUST_USE_OFFICIAL_KB` | No | Forbidden content nature (official regulation claim); no URL/date/provenance/reviews/signoff |
| `CM-017` | `DG-SERV-03` | `campus_services` | `FACTUAL_CLAIM_UNVERIFIED` | `NEEDS_OFFICIAL_VERIFICATION` | No | Forbidden content nature (unverified factual claim); no URL/date/provenance/reviews/signoff |
| `CM-018` | `DG-STUDY-01` | `studying` | `STUDENT_EXPERIENCE_GUIDANCE` | `NEEDS_MODERATION_REVIEW` | No | Moderation unapproved; no URL/date/provenance/privacy/rights/signoff |

## Path to Publishing Eligible Community Items

1. Keep `CM-010`, `CM-011`, `CM-012`, `CM-015`, `CM-016` permanently excluded from Community Knowledge (must be answered via Official Knowledge only).
2. Keep `CM-013` and `CM-017` excluded until verified against official KU sources.
3. Keep `CM-001`, `CM-004`, `CM-006` blocked under `BLOCKED_SENSITIVE_DEFAMATION_REVIEW`.
4. For remaining opinion/guidance candidates (`CM-002`, `CM-003`, `CM-005`, `CM-007`, `CM-008`, `CM-009`, `CM-014`, `CM-018`), publication requires sourcing verified public `https://` post URLs, ISO publication dates, privacy/rights reviews, accountable moderator signoff, and reputational review for named-person items (`CM-002`..`CM-009`).
