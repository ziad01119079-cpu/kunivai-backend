# Kuniv AI adaptive answers and model readiness

## Baseline and scope

Development branch: `codex/rag-quality-sprint-1`. The worktree started clean at `c699cfe790c43f957a53bc50541121eba36deb34`, the adaptive candidate built on Sprint 2 (`268c29bdce0685cd6793aaf0d6c06aed31498f2d`). The supplied task defines the scope. The Antigravity evaluation, scorecard, routing analysis, migration recommendation, and quarantine manifest provide evidence and proposals. The separate scalability worktree was outside the change.

The code keeps the adaptive Arabic and English answer guidance, previous-answer expansions, full mixed turns, structured schedules, math and examples, calendar scope, and answer-first sourcing. It retains existing Redis idempotency, admission, SSE replay, and Firestore persistence contracts. The candidate adds no classifier calls.

## Changes and decisions

| Case | Candidate behavior |
| --- | --- |
| Q015, editing banking information | Gives the verified general SIS entry and directs the student to Student Affairs. The exact request has a deterministic reply. Related generated answers receive a direct-evidence requirement. |
| N07, course withdrawal with a teaching request | The service delivers the applicable official date and citation, then continues with the untouched question for the distinction between withdrawal from a course and full withdrawal. The source stays in the final SSE citations. The frozen 24 October 2026 example is marked past on the November fixture date. |
| Q076, bus registration | Gives the home-transport application entry for female students and the separate campus transport contact. The KU transport department describes the inter-college service. |
| Q002, first-year Law finals | Preserves the prior finals intent, leaves the date unverified, and puts the Law guidance contact in both the answer and final citation metadata. |
| U02, U12 and similar | A rule or service request routes to official retrieval even when it contains a possessive or a teaching subtask. Personal-only questions and general lessons retain their routes. The full mixed question reaches generation. |
| Retrieval and SSE | A completed File Search with missing or inapplicable citations can trigger the normal Web Search fallback. An incomplete stream produces a typed terminal error and stops the attempt sequence; buffered unverified text stays private. Streamed provider-only citation markers are removed across chunk boundaries, with structured annotations retained and offsets adjusted. |

The reference report described ceilings from a different revision, including a 220-token direct answer cap. This work keeps the adaptive base ceilings already on the branch, including 480 for direct questions, 1,800 for contextual expansion, 2,200 for requested detail and 2,400 for mixed requests. Academic rules that ask for conditions or penalties have guidance and room for multiple clauses. Hosted tools receive an additional 180 tokens beyond the existing 850-token minimum when applicable. Explicit low reasoning receives 600 more tokens. These are safety ceilings; answer length still comes from the prompt. The new values stay within the existing provider admission envelope. Their effectiveness for live models needs measurement.

Official-claim validation still rejects legacy File Search annotations without independently verified publisher metadata. The official Web Search fallback may therefore remain necessary until the knowledge base has reviewed provenance. If both retrieval paths finish without usable evidence, that turn can still end in a general evidence-gap response, including for a mixed question. Verify those cases explicitly in staging. The application does not independently validate the factual entailment of every sentence returned with an official URL.

The [official campus transport page](https://gsd.ku.edu.kw/ar/departments/transportation-section), [official home transport instructions](https://gsd.ku.edu.kw/ar/services/home-transportation) and [Law guidance office](https://law.ku.edu.kw/ar/students/guidance-and-counseling) were available as indexed KU pages. Direct fetches through the browsing tool returned 502 at verification time. The [SIS entry](https://portal.ku.edu.kw/sis) redirected to the JavaScript login landing page; it established the general portal URL only.

## Optional staging configuration

Leave `OPENAI_ROUTE_MODELS_ENABLED` unset or false for existing behavior. Chat continues to take its model from the current `OPENAI_MODEL` setting. The application's existing development default for that variable is unchanged. Example isolated staging settings after approval:

```dotenv
OPENAI_MODEL=gpt-4o
OPENAI_ROUTE_MODELS_ENABLED=true
OPENAI_MODEL_DEFAULT=gpt-6-luna
OPENAI_MODEL_DETAILED=gpt-6-luna
OPENAI_MODEL_RAG=gpt-6-luna
OPENAI_MODEL_RAG_FALLBACK=gpt-5.4-mini-2026-03-17
OPENAI_REASONING_EFFORT_DEFAULT=none
OPENAI_REASONING_EFFORT_DETAILED=low
```

The fallback model overrides only the existing Web Search pass following a completed but unusable File Search result. It does not introduce a model retry on errors or truncation. Blank settings inherit the normal chat model. The SDK receives `reasoning` only for the documented Luna and GPT-5.4 mini IDs, with this candidate allowing `none` and `low`. Existing schedule extraction and background calls retain their own legacy configuration. The installed OpenAI Python SDK 3.35.1 exposes the Responses API model, reasoning, hosted tools, tool limits, and output ceiling parameters. Provider model access and this project's live tool/effort combinations remain unverified. [GPT-6 Luna capabilities and pricing](https://developers.openai.com/api/docs/models/gpt-6-luna) · [GPT-5.4 mini capabilities and pricing](https://developers.openai.com/api/docs/models/gpt-5.4-mini)

Telemetry captures observed provider usage including reasoning tokens, already included in reported output tokens. Model prices provide estimates for standard text usage and observed hosted-tool calls. Luna cache writes and account-specific tier adjustments are outside the present estimate. Verify invoices separately. The external comparison has 120 model-question rows over 40 questions and a separate 16-question RAG pass. Its results support staging hypotheses. Swapped, stale, or incomplete evidence prevents using the composite model score as a production accuracy estimate.

## Offline verification

| Check | Result |
| --- | --- |
| Backend suite outside Firestore integration | 272 passed, plus 11 passing subtests, covering chat retry, replay, distributed idempotency and rate/queue protections |
| Firestore integration | 7 passed against a dedicated loopback Firestore emulator using a demo project and mocked authorization, FCM, and OpenAI |
| Sprint 1 evaluation | Development 16/16, challenge 8/8, holdout 8/8 |
| Sprint 2 evaluation | 17/17 request fixtures and 4/4 official-calendar HTML fixtures |
| Adaptive dry comparison against c699cfe | All 39 fixture pairs prepared. 37 retained type and base cap. Q010 moved from simple 480 to academic 850. Q070 moved from academic 850 to policy rules 1,150. |
| Focused regressions | Audited scenarios, mixed routing, calendar citation, previous-turn expansion, detailed STEM, model options, sanitized SSE markers/offsets, interrupted or truncated retrieval, and legitimate fallback |

The adaptive comparison exercises policy and prompt inputs. The generated text in the new tests comes from mocks. Live answer quality, output token use, cost, and latency remain unverified for the consolidated candidate.

## Independent staging validation

1. Freeze anonymized held-out questions, expected subtasks, applicable official evidence, and reviewer rubric before running. Include separate groups for current evidence, stale evidence, missing evidence, wrong faculty/audience, mixed intents, Arabic follow-ups, and detailed STEM. Keep the historical 39 paired questions for continuity and score them separately from held-out questions.
2. Complete the source review in [the 32-file remediation plan](kuniv_vector_store_remediation.md). First use frozen policy evidence to compare the legacy revision and the candidate on GPT-4o. After that comparison, test Luna `none`, Luna `low` on detailed cases, and the optional GPT-5.4 mini Web Search fallback against the same candidate and evidence. Change one variable at a time. Use isolated staging credentials, an isolated Redis/Firestore fixture, and the reviewed staging vector store for the end-to-end pass.
3. Obtain fresh approval for the call count and hard dollar ceiling. Budget and track full input, output, hosted-search fees and existing fallbacks. Disable SDK retries in the evaluator. Test a small provider compatibility set before the paired run, and stop on missing usage, an unexpected attempt, or inadequate remaining authorization.
4. Blind reviewers to variant and model. Score claim correctness, every subtask, usefulness, verbosity, citation entailment/applicability, and whether abstention includes a practical next step. Measure attempts, hosted calls, input/cache/output/reasoning tokens, truncation, provider TTFT, first verified text and terminal latency. Report missing or stale evidence separately; disagreements remain unverified until adjudicated.
5. Require no high-risk unsupported academic claims, no critical usefulness regressions, and applicable citations in every verified calendar fixture. Verify one logical response per request, interrupted SSE replay without extra dispatch, a terminal event, and rate/budget handling on staging. If the candidate model fails, disable the route flag and retain the staged GPT-4o configuration. Release and Vector Store promotion remain separate approvals.

This work used no new paid OpenAI calls and did not touch production settings, data, Flutter, or the Vector Store.
