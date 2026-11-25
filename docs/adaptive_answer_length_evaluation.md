# Kuniv AI adaptive answer length evaluation

## Scope

Baseline: Sprint 2 commit 268c29bdce0685cd6793aaf0d6c06aed31498f2d. Candidate: the implementation in this branch. The paid comparison uses all **29** audit cases previously marked overlong, paraphrased as synthetic prompts, plus **10** frozen cases covering short facts, detailed explanations, worked examples, follow-ups, and mixed requests. The original student records stayed in the restricted audit folder. The 29 historical lengths are descriptive because they were recorded on older deployments and are outside the paired A/B comparison.

GPT-4o returned the same model snapshot for both variants. Both used the same question, context, evidence summary, date, and temperature. The harness ran the actual application prompt and output ceiling for each variant with hosted search and automatic retries disabled. This isolates the answer-length prompts, assuming the same supplied evidence. It does not exercise production retrieval, deterministic calendar routes, or the Responses SDK's hosted citation annotations. An independent mock regression checks that an existing inline source is not repeated and stays in SSE done metadata.

The candidate was refined on these fixtures after reviewing two earlier trials. Treat the final result as an in-sample comparison. No production test or independent live holdout was run.

## Results

| Metric | Baseline | Candidate |
| --- | ---: | ---: |
| Output tokens, 39 prompts | 4,873 | 4,773 |
| Output tokens, four short fact/link cases | 75 | 63 |
| Output tokens, 29 historical overlength cases | 2,145 | 2,350 |
| Completed requested content, reviewer judgment | 31/39 | 31/39 |
| Useful, reviewer judgment | 31/39 | 30/39 |
| Avoidably verbose, reviewer judgment | 13 | 12 |
| Unsupported material, reviewer judgment | 5 | 5 |
| At least one expected clickable URL where evidence was supplied | 9/23 | 15/23 |
| Token-limit truncation | 1 | 0 |

- The derivative follow-up added a worked expression with **307 → 280** tokens. The English inequality follow-up retained the reason and a numerical example with **404 → 141** tokens.
- The explicit fraction explanation kept two correct worked examples. The Queue/Stack answer completed with its practical examples, avoiding the baseline's token-limit interruption.
- **156 GPT-4o attempts** across the final paired evaluation and two tuning rounds cost an estimated **$0.794** against the approved **$3**. All attempts used gpt-4o-2024-08-06. No hosted searches or automatic retries ran.
- **251 tests and 11 subtests passed**, including the prompt, follow-up, citation deduplication and output-ceiling checks.

A completion judgment concerns the requested task, including a requested calculation, concrete next action, or honest uncertainty where official evidence is absent. Usefulness is a separate reviewer judgment about whether the student can act or understand from the output. The per-case scorecard flags avoidable repetition, unrelated expansions and unnecessary introductions. A URL coverage check requires an expected source to be clickable where evidence was supplied. An evidence-appropriate URL still does not guarantee every claim is supported.

## Candidate changes

- Factual and link-only requests lead with the fact or the clickable link. Academic dates and rules lead with the answer and keep essential scope, exceptions, and the official source.
- Educational, step-by-step, example, comparative and mixed requests keep the explanation and any requested calculations or examples. An explicit follow-up uses prior answer context to expand the relevant point. A new example on a named topic does not inherit an unrelated prior reply.
- Concision is a prompt instruction. Output ceilings provide headroom for detailed responses, mixed requests, and retrieval; they are not the primary length control. The existing incomplete-response handling is retained.
- A URL already rendered as an inline Markdown link is not repeated in a Sources appendix. Structured citations and new links remain available.

## Decision and limits

**Keep the candidate in development.** The four short fact and link cases used fewer tokens and the follow-ups were more focused. The broader historical group did not use fewer tokens than current Sprint 2. Applicable-link coverage improved, but the evaluated candidate still inferred that bank details could be edited in SIS without supporting evidence (Q015), omitted the date citation in the mixed withdrawal question (N07), and assumed the requested bus was the home service (Q076). These issues preclude a release recommendation. The paired model test bypasses calendar and direct-answer protections that operate in production, so this is evidence about the prompt candidate and its limits.

No deployment or push. No live student data was sent to GPT-4o. The Firebase notification integration suite was not run. Provider charges are calculated from reported token usage and standard rates, so they are estimates. [GPT-4o pricing](https://developers.openai.com/api/docs/models/gpt-4o)
