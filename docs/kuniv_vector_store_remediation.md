# Kuniv AI Vector Store Remediation Plan

This is a review and execution plan for a future isolated staging index. The 32 entries below come from the supplied Antigravity quarantine manifest. No source or index was changed. Filenames and manifest flags identify work for reviewers. Each replacement still needs a provenance and applicability check before it enters the staged official index.

## Execution and rollback

1. Export the current index inventory and per-file attributes to restricted storage. Record vector-store ID, file IDs, checksums, ingestion status, source URLs, dates, and last known-good application configuration. Keep all originals and the production index intact.
2. Clone eligible content into a separate staging vector store. Exclude both swapped files and the unofficial commentary immediately from this clone. The two replacements in the table identify the *actual* topics in the swapped content. Rebuild from primary KU publications and verify titles against text.
3. For each replacement record publisher and canonical HTTPS URL, original publication/effective date, academic year and semester when relevant, faculty, audience, event or policy topic, capture date, checksum, and reviewer. Mark historical versions explicitly, keep future calendars out of default current-year retrieval, and keep rules separate from date announcements. For files with an instruction to deduplicate, compare both content and applicability before choosing the retained copy.
4. Normalize new filenames to NFC and remove U+061C, U+200E–U+200F, U+202A–U+202E, and U+2066–U+2069. Legacy names in the manifest's cleaned column still include U+2069 in several rows. Re-extract OCR only after comparing tables, exception lists, and cross-month dates with the primary document. Hold any unverified replacement outside the staged official index.
5. Confirm ingestion completes, every manifest row has one reviewed disposition, and no quarantined file ID appears in the cloned store. Verify positive retrieval for the corrected topics and negative retrieval for stale, future, wrong-faculty, wrong-audience, and unofficial material. Compare returned passages and citations with the primary source and record where evidence is genuinely missing.
6. Any future promotion needs its own approval and a reviewed old-to-new source map. Retain the previous store and application configuration through the rollback window. Roll back by restoring the prior vector-store setting in the relevant environment and running a source-backed smoke test. Avoid deletion during correction or promotion.

## Source gaps to review separately

The independent review flagged incomplete evidence for H02, U01, H11, U16, H18, and U17, and stale evidence for H04 and H06. U02, H15, H16, and H17 had partial or incomplete answers in that review. Confirm each gap with current official KU material. Where no applicable official evidence exists, retain an actionable uncertainty response. The reports do not establish the quality of future retrieved answers or that every cited file supports the claim.

## 32-file disposition

“Reverify” reproduces the manifest's explicit flag. Every staged replacement still receives the applicable checks above.

| Priority | Current file ID | Staging disposition | Proposed staged name or archive path | Reverify |
| --- | --- | --- | --- | --- |
| CRITICAL | file-X27Q4ZNS2x86TYCZBqmXG7 | Exclude original; rebuild this topic from its matching official policy | التخصص_النادر_شروط.md | YES |
| CRITICAL | file-Ggm7bGmCzK83NhkdkaufHB | Exclude original; rebuild this topic from its matching official policy | التبادل_الطلابي_الاكاديمي.md | YES |
| CRITICAL | file-SLjmY3f6joZnhVUXuaMhG7 | Exclude unofficial commentary from the official index; keep private archive | Private archive only | NO |
| HIGH | file-QLeiMmEJVBoTAr5orfCRpj | Verify dates and semester against original official publication; fix filename | التقويم_الفصل_الاول_2026_2027.pdf | YES |
| HIGH | file-WTMRGiDCD5Hewmona7Rs5h | Re-extract verified current edition or retain this edition as historical only | دليل_الطالب_2025_2026.pdf | YES |
| HIGH | file-JKyFrpEHwWxNjWzfkDeySH | Re-extract as structured Markdown; review tables and dates against the official PDF | دليل_قواعد_القبول_والتحويل_2026_2027.md | YES |
| MEDIUM | file-NU14wZZq9wyWnZaVRND6Bb | Fix filename and heading; confirm title describes the content | الفرقة_الدراسية.md | NO |
| MEDIUM | file-DWxXteJrAASP4MkrENbbch | Keep verified procedure separate from cycle-specific aptitude-test dates | نظام التسجيل الإلكتروني لاختبارات القدرات الأكاديمية.md | YES |
| MEDIUM | file-EmLUwedZfp1QZMbziUMdSt | Tag 2027/2028 and applicable semesters; exclude from current-year retrieval | التقويم_الأكاديمي_2027_2028.md | YES |
| MEDIUM | file-Pdzqh4qgoRjmEF7zBpNx8S | Keep in historical retrieval only with explicit cycle/faculty tags | archive/النسب الدنيا لقبول الفصل الدراسي الأول 2025.pdf | YES |
| MEDIUM | file-2VfSyfRwk8XeJTWsiJvuVN | Compare source edition and hashes; keep one version per scope and effective period | الخطة الزمنية قبول ٢٠٢٦ (5).pdf | YES |
| MEDIUM | file-UqY6kVgaVsvpbLvueA7KYm | Compare source edition and hashes; keep one version per scope and effective period | دليل القبول (1) (2).pdf | YES |
| MEDIUM | file-16mq3apjryABuw3u3goqGK | Compare source edition and hashes; keep one version per scope and effective period | StudentGuideAR (1).pdf | YES |
| MEDIUM | file-S9KEjWZiHDAtzZD5axDjhk | Keep in historical retrieval only with explicit cycle/faculty tags | archive/كلية الاداب الفصل الثاني ٢٠٢٥-٢٠٢٦.pdf | YES |
| MEDIUM | file-Taa5PyA2n88tTfhvR92Q9D | Keep in historical retrieval only with explicit cycle/faculty tags | archive/دليل التحويل الجديد 2025.pdf | YES |
| LOW | file-Ubd9cdMqqnEYihxKKKYxp4 | Normalize filename and heading; confirm unchanged content | التخصصات_النادرة_في_جامعة_الكويت.md | NO |
| LOW | file-QB27ixrWuspyFTxe4AupJT | Normalize filename and heading; confirm unchanged content | التقويم_الدراسي_2026-2027.md | NO |
| LOW | file-9Dq37GaXDB6NwaEkBzgPWx | Normalize filename and heading; confirm unchanged content | دليل_التقديم_وماجستير_جامعة_الكويت.md | NO |
| LOW | file-N26nSTqg2PQnF68kdLcTv3 | Normalize filename and heading; confirm unchanged content | الخطة_الزمنية_لتقديم_طلبات_الالتحاق_بجامعة_الكويت.md | NO |
| LOW | file-YB8MdCwFLaF7Y2b5sRq2xm | Normalize filename and heading; confirm unchanged content | الحدود_الدنيا_للقبول.md | NO |
| LOW | file-TsGvSTMESByExbWPHu8278 | Normalize filename and heading; confirm unchanged content | الانقطاع_عن_الدراسة_وتأجيلها.md | NO |
| LOW | file-6VApYMES37sKPKuTr1fn3M | Normalize filename and heading; confirm unchanged content | قواعد_تأجيل_الامتحانات.md | NO |
| LOW | file-6fa8pfwPm91UKQDYVVTZ67 | Normalize filename and heading; confirm unchanged content | قواعد_الانسحاب.md | NO |
| LOW | file-AXJURwmr3yjEX3nJbivtXu | Normalize filename and heading; confirm unchanged content | العبء_الدراسي_الاعتيادي.md | NO |
| LOW | file-W3Ukc52DFkx1uUGAPjVUXb | Normalize filename and heading; confirm unchanged content | ضوابط_تغيير_التخصص_الجامعي.md | NO |
| LOW | file-TbD6cq4rRDVZ4qeCR4XFvq | Normalize filename and heading; confirm unchanged content | نظام_ترقيم_المقررات_الدراسية.md | NO |
| LOW | file-1GgEE5qxQaeKJ3GQXms77v | Normalize filename and heading; confirm unchanged content | لائحة_الغش_في_الامتحانات.md | NO |
| LOW | file-UwNMknnUNB6x64NH7PrQLJ | Normalize filename and heading; confirm unchanged content | لائحة_الفصل_الصيفي.md | NO |
| LOW | file-8Q6W81nFsZLi7bthJV6h5J | Normalize filename and heading; confirm unchanged content | شروط_وقواعد_الإنذارات_الجامعية.md | NO |
| LOW | file-7Keao4owgEB6mYLavzJne8 | Normalize filename and heading; confirm unchanged content | المدة_الاعتيادية_للتخرج.md | NO |
| LOW | file-UWX9k1KMowwSHRwcB7Vbcz | Normalize filename and heading; confirm unchanged content | شروط_ونسب_قبول_جامعة_الكويت.md | NO |
| LOW | file-T7kPwhWEep4Vi1F7PLgNmT | Correct filename; confirm content identity | معلومات_التواصل_كلية_الآداب_قسم_الإعلام.md | NO |
