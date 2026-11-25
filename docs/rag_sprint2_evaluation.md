# Kuniv AI RAG Sprint 2 evaluation

## Decision and scope

**Offline quality gate: PASS.** The confirmed Sprint 1 routing/context regressions and the real KU Spring transfer parsing defects passed the identical-fixture comparison. A live-model quality, billing, and latency decision remains outside this evidence.

Development branch: codex/rag-quality-sprint-1. Compared against Sprint 1 commit ea8eb3d1a5d445c4feb5ff2010781cfad415d81e. The Sprint 2 request cases were fixed before implementation. The request set contains the 11 quality-gate failures plus six synthetic cases covering a referenced file, Arabic pronoun follow-up, a date-plus-explanation request, guide links, arithmetic and a useful Law abstention.

All chat comparisons mock Firestore, calendar fetches, the generation stream and background analytics. The mock sends a scripted explanatory continuation, making tests able to detect dropped tasks, missing context and citation errors. It does not grade independently generated GPT-4o prose. Source-date checks use actual official KU HTML captured November 25, 2026. The fixtures retain relevant cards, rows and their section headings. Full captured pages were also verified with the parser. There was no deployment, Flutter release, production student-data access, or paid OpenAI invocation during this sprint.

## Identical-fixture comparison

| Check | Sprint 1 | Sprint 2 |
| --- | ---: | ---: |
| Known regression cases | 0/11 | 11/11 |
| Additional request cases | 3/6 | 6/6 |
| Real KU calendar checks | 1/4 | 4/4 |
| Missing requested continuations | 11 | 0 |
| Avoidable abstentions | 6 | 0 |
| Mock provider attempts | 3 | 14 |
| File Search offers | 1 | 2 |
| Web Search offers | 0 | 0 |
| Estimated submitted text tokens | 4,274 | 21,849 |
| Local median first chunk | 4.7 ms | 16.5 ms |
| Local median completion | 4.9 ms | 17.5 ms |

The existing Sprint 1 development, challenge and holdout sets scored **32/32**. Backend verification reported **231 tests and 11 subtests passed**.

The missed-subtask measure is the count of requests in which the expected explanatory continuation is absent. Avoidable abstentions are the five known misplaced user-facing disclaimers on general teaching or verification requests plus the unavailable-date message on a known withdrawal deadline in the mixed-date case. The valid first-year Law uncertainty was measured separately for its next step. Correctness in this report covers deterministic data and routing/citation contracts. Live model completeness remains unverified.

## Worked examples

- **Guide plus preparation.** Sprint 1 sent the guide link and ended. Sprint 2 emits the same verified link, keeps the student's whole request, and continues to a model answer for preparation. The fixture confirms the link and the scripted remaining steps both survive SSE. [KU New Student Journey](https://www.ku.edu.kw/ar/new-student-journey)
- **Registration plus adding a course.** The verified SIS link is shown first. KU-specific instructions still require separately accepted evidence. A negative test made the model cite the University of Kansas on both retrieval attempts. Only the official KU link and an uncertainty message reached the student. [KU enrolled-student FAQ](https://www.ku.edu.kw/sites/default/files/2025-08/FAQAr.pdf)
- **GPA and warnings.** A formula-plus-example request retains the sourced formula and reaches generation for the example. A question already giving GPA and credit weights bypasses the grades-only canned clarification. The prompt instructs the model to reuse those inputs and clarify only the unknowns. Withdrawal, transfer, warning policy and general university concepts are resolved separately. [KU course regulations](https://shi.ku.edu.kw/ar/courses-system-list)
- **General assistance.** Gemini programming, a programming topic switch after a Gemini access question, institutional affiliation as a concept, and midterm versus finals now reach normal academic generation. The subject of a previous turn is inherited only by recognized continuations. The Arabic study-plan follow-up retained both the original history and its structured schedule. The prior-file case asks for the content when the file itself is unavailable instead of inventing a summary.
- **Specific Law finals.** The Law calendar does not verify a Fall 2026/27 date for first-year finals. The response gives the official Law guidance contact. It does not substitute the general finals dates or label a January vacation as an examination window. [Law calendar](https://do.ku.edu.kw/ar/lawcalender) · [Law guidance](https://law.ku.edu.kw/ar/students/guidance-and-counseling)

## Official calendar behavior

- Event cards and table rows bound the dates to the specific event. The resolver verifies official KU origin, academic year, semester, faculty and requested audience. Within a published Spring section it checks an early-June transfer window against the Summer start where available. Reversed, contradictory or wrong-year dates stay unverified rather than being repaired or carried from another row.
- The Spring transfer periods **June 1–3, 2027** and **May 30–June 1, 2028** now verify. The January 15, 2027 probe selected Fall using published term starts. The January 1 heuristic had returned a Spring deadline on Sprint 1. When the source does not provide a complete set of term starts, the month-based default is still available, subject to matching an applicable source row.
- The general course-withdrawal date remains **October 24, 2026**. Law uses **October 22, 2026** for its course-withdrawal and optional enrollment-suspension row. Past dates are explicitly described as passed. The general full-semester-withdrawal row actually publishes November 26, 2025 under Fall 2026/27. It is marked conflicting, with the Deanship link for confirmation. [General calendar](https://do.ku.edu.kw/ar/academic-calendar-0) · [Law calendar](https://do.ku.edu.kw/ar/lawcalender)

## Resource interpretation and limits

The focused comparison intentionally uses more mock generation where Sprint 1 had stopped early. The input estimates use the same local tokenizer and fixtures. They count submitted text and exclude provider-added tool material and hidden input. Tool offers show routing. No hosted tool executed. Mock TTFT and duration include local parser, prompts, mocks, and runtime overhead from a single pass per fixture, with the tokenizer prewarmed. They cannot predict production latency or student-visible rendering.

Standalone link and arithmetic fixtures remain provider-free. The 16 original development cases retain their zero-provider paths. Standalone educational requests without attached context can still omit unrelated schedule data. Follow-ups conservatively retain structured data when relevant, so their token cost can increase. Existing attempt telemetry, Redis admission, SSE/persistence contract, rate limits and provider model selection remain unchanged.

Source controls cannot prove every statement of a live model from an approved-domain URL alone. Real generation may still miss an implicit compound request or require better claim-to-passage verification. Keep the frozen tests when measuring separately sampled live questions if that work is later authorized. No broad architecture or rollout work is included in Sprint 2.

## Reproduction

Run the same harness from the Sprint 2 checkout against both app trees. An isolated baseline can be prepared from the Sprint 1 commit using git archive of its app directory.

~~~text
python eval/run_rag_sprint2.py --backend path/to/sprint1
python eval/run_rag_sprint2.py
python -m pytest tests -q --ignore=tests/integration --basetemp path/to/writable/temp
~~~

The guarded Firebase notification integration suite was not executed.
