# Kuniv AI RAG Sprint 1 evaluation

## Decision

Sprint 1 implementation is complete on codex/rag-quality-sprint-1 at f004b9a07b2881c27b91ed1a779395cac9791d1d. Offline results support a release candidate, with a production release recommendation deferred. No production configuration, deployed model, admission budget, Redis coordination, Firestore, or client protocol was changed. No paid OpenAI model was invoked.

## Baseline and method

- Reviewed the November 24 First 100 audit and the owner-only dataset, scorecard, routing cost, and selection metadata. The cohort represents October 10–November 22 saved answers. 16 material-defect rows informed the development regression set. Cases use paraphrased prompts and synthetic histories. No student transcript or account identifiers entered the repository.
- Both checkouts used the same mocked provider, fixed October 24, 2026 Kuwait date, and fixture calendar pages. Baseline code came from df6c371d0936e4e152659f0169293d0436eb62a1. The mock exercises orchestration with plausible unsafe answers and missing evidence. It cannot evaluate what a live model would generate.
- The development set has 16 audit-linked cases. Eight synthetic challenge cases were used during debugging and are reported separately. Eight new synthetic holdout cases and scoring predicates were frozen in the implementation commit before scoring. No implementation changes were made after scoring that set. A harness condition was repaired before the holdout ran. The holdout avoids reuse of student text and tuning on its results. Its small fixture-based scope limits generalization to actual student questions.
- Factual assertions are scored only where reference facts exist. Clarifications and abstentions are judged for usefulness and evidence handling, with factual accuracy left unset. Source applicability checks expected institutional sources, rejection of wrong events or institutions, and scope behaviors. Citation checks include official origins and actual clickable inline links for the guide cases. Completeness uses predetermined required elements, and the length cap is 95 words for these short requests. These dimensions are fixture-specific checks, and 95 words is not a length policy for all queries.

## Measured offline results

| Fixture split | Pass all checks, baseline → Sprint 1 | Provider attempts | Submitted text tokens |
| --- | ---: | ---: | ---: |
| Development | 3/16 → 16/16 | 25 → 0 | 31,719 → 0 |
| Challenge | 3/8 → 8/8 | 8 → 6 | 12,328 → 8,789 |
| Holdout | 1/8 → 8/8 | 12 → 1 | 16,498 → 2,094 |

| Metric | Development baseline → Sprint 1 | Holdout baseline → Sprint 1 |
| --- | ---: | ---: |
| Factual accuracy on verifiable cases | 3/9 → 9/9 | 1/4 → 4/4 |
| Completeness | 3/16 → 16/16 | 1/8 → 8/8 |
| Source applicability | 4/16 → 16/16 | 5/8 → 8/8 |
| Citations | 4/16 → 16/16 | 5/8 → 8/8 |
| Length threshold | 16/16 → 16/16 | 8/8 → 8/8 |
| Usefulness | 3/16 → 16/16 | 1/8 → 8/8 |

- One synthetic educational request with an unrelated 40-course schedule used one provider attempt in each version. Submitted client text fell from **2,038 to 1,414 tokens**, and the schedule component went from 700 to 20 tokens. The same case met every quality check in both versions. System text was 1318 → 1374 tokens and no history was attached to this fixture.
- A personal schedule question went from two hosted-retrieval offers to one no-tool model attempt. The actual schedule component stayed at 700 tokens in the served model attempt, and the response passed the schedule fixture. The deterministic arithmetic case went from two provider attempts to zero.
- Backend regressions: **210 tests and 11 subtests passed**. The notification integration suite requires its own isolated runner and was not executed.

The comparison applies to the fixture scenarios only. The original 16 historical defects were not re-generated, and no improvement percentage is claimed for the full 100 or production. All non-notification-integration pytest cases passed in the local environment. The guarded Firebase notification workflow was not executed. An initial unqualified pytest run stopped at its isolation guard. A subsequent run used an explicit writable pytest temp directory for local TLS tests.

## Implementation and evidence

| Audit failure | Implemented behavior | Verification |
| --- | --- | --- |
| Holiday, transfer, withdrawal and enrollment dates were conflated | Match the event to its own calendar table row, or keep a flat general calendar date within the named event block. Require the requested year, term and official KU origin. Report conflicting/missing evidence without inventing a date. | Q001/Q002, Q029–Q031, Q068, H02/H03 and neighboring-row tests. |
| Law first-year finals were generalized | A first-year exam request requires an explicit first-year match in the dated source block. Links to calendars checked can accompany an explicit uncertainty statement. | Positive first-year row and negative generic exam row tests. |
| Academic warning was inferred from an altered grade set | Ask for cumulative GPA, GPA credits, credit weights and the warning type. State the verified weighted-average method for major GPA without converting a hypothetical term GPA into a standing decision. | Q005, H07, T06. [KU enrolled-student FAQ](https://www.ku.edu.kw/sites/default/files/2025-08/FAQAr.pdf) · [KU regulations](https://shi.ku.edu.kw/ar/courses-system-list) |
| Kansas payment procedure was attributed to KU | Ask which service the user means for a generic account problem. Drop Kansas citations and require a KU origin for the supported high-risk account/policy intents. Withhold buffered answers when a citation failed that gate. | Q014, H04 and authority checks for lookalike domains, credentials, scheme and port. [KU IT support](https://cis.ku.edu.kw/ar/contact-us) |
| The guide link was a filename and Arts was confused with Education | Return clickable links for the new-student guide and direct registration route. Label the Arts Arabic major sheet with its applicable entrant cohort. | Q006, Q099/Q100, T05. [KU new student guide](https://www.ku.edu.kw/ar/new-student-journey) · [KU Arts major sheet](https://arts.ku.edu.kw/sites/default/files/2026-09/StudnetGuide25-26.pdf) |
| App affiliation, AI tools and student access were asserted without evidence | State that affiliation is unverified. A published Gemini workshop supports the existence of KU training. Account eligibility, the sign-in link and code workflow remain unverified and the answer links to KU IT. | Q025, Q066/Q067, Q073, T04/T08. [KU workshop](https://afaq.ku.edu.kw/ar/media-center/news/n3-61) |

The prompt also instructs the model to match institution, faculty, term, effective date, event and audience. The HTTP/file citation gate enforces source origin and usable URL shape for its covered query class. It cannot independently prove that every claim in a web-backed answer follows from the cited page. Legacy file citations lack verified publisher metadata, so covered high-risk asks fall back when the file annotation alone cannot prove authority. General file/web fallback precedence and the answer verification buffer remain. A personal schedule request still has access to its full modeled schedule context. Short Arabic calendar follow-ups use recent relevant user turns. Unambiguous two-operand arithmetic skips the provider. The replay result of 81 uncertain historical questions was not used as a blanket rerouting rule.

## Token investigation and cost observability

The audit's median of 18,594 input tokens came from 36 historical final usage records. Those records lack per-attempt prompts, exact retrieval input, cached token counts, and most tool events. There is no defensible decomposition of the historical median. The baseline code defaults to a 3,000-token history budget. It also inserts a structured payload containing all saved schedule versions into both system prompts, and the Web Search fallback resends the question and available history. The historical runtime values and hosted provider overhead are unknown.

This implementation logs per-attempt client-side counts for system instructions, schedule, client context, history and current question. It records File Search returned passage counts and estimated passage tokens when exposed, along with executed tool IDs counted internally and omitted from the logs. Provider billed input minus local component estimates is a residual that can include hosted retrieval, tool instructions, message framing, caching semantics and tokenization boundaries. Web Search passage size is unverified when the provider does not expose it. File Search passage totals are estimates of returned text, not the provider's billable input split. The local benchmark measures text submitted to mocks and does not attach a made-up value to hosted content.

Per-attempt telemetry logs the requested and provider-returned model, input/output/cached usage where present, observed hosted search call counts, fallback and retry reasons, and outcomes. The background classification call joins the trace and emits an updated cumulative request summary. The foreground and including-background summaries are cumulative views and must not be added. Unknown models or usage stay unpriced. Missing cache counts are labeled as an uncached price assumption. A lowercase server-generated trace ID joins cost events without recording question text, schedules, attachments, credentials, UID, session ID, or caller-provided correlation. Responses SDK file-citation filenames are used when present to skip redundant metadata requests. The existing transport quota reservation remains independent of observed usage.

The GPT-4o standard pricing used is $2.50 uncached, $1.25 cached and $10 output per million tokens. GPT-4o-mini has its own price row. The estimates include observed tool fees only. Search content can be charged at model rates according to provider pricing. Unreturned usage, provider-side retries, tool status ambiguity on interruption, adjustments, and unknown selected model snapshots prevent exact billing attribution. The distributed client already disables SDK auto-retries. Outside distributed mode transparent SDK attempts may be invisible to application attempt telemetry. [GPT-4o pricing](https://developers.openai.com/api/docs/models/gpt-4o) · [GPT-4o-mini pricing](https://developers.openai.com/api/docs/models/gpt-4o-mini) · [Tool pricing](https://developers.openai.com/api/docs/pricing)

## Remaining release validation

- Review a fresh, separately sampled production-quality cohort with applicable official policies and current KU sources. Compare live provider accuracy and usefulness only with an approved budget, using fixed citations and independent adjudication for disagreements.
- Validate current KU HTML layouts and primary-source availability on a release candidate. The parser remains conservative for unsupported audiences, inconsistent years, unrecognized layouts and missing ranges. Check false abstentions and verified alternative sources during staging.
- Verify completeness of provider events and aggregate costs against same-period project and tool-level billing. Validate production overhead from token counting and monitor schedule/context distribution. No live provider latency, production savings, or exact billing attribution was measured.
- Preserve the versioned fixtures but keep future holdout cases out of tuning. The 100-row historical audit remains the development baseline.

## Reproduce

Use the Sprint 1 checkout's runner and evaluation fixtures for both revisions. Export the baseline app source to a separate directory with git archive at df6c371d0936e4e152659f0169293d0436eb62a1.

~~~text
python eval/run_rag_sprint1.py --backend path/to/baseline --split development
python eval/run_rag_sprint1.py --split development
python eval/run_rag_sprint1.py --backend path/to/baseline --split challenge
python eval/run_rag_sprint1.py --split challenge
python eval/run_rag_sprint1.py --backend path/to/baseline --split holdout
python eval/run_rag_sprint1.py --split holdout
python -m pytest -q tests --ignore=tests/integration --basetemp path/to/writable/pytest-temp
~~~
