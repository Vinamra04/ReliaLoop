# ADR 0003: LLM provider and model choice

- **Status:** accepted
- **Date:** 2026-11-12

## Context

The agent's agentic mode needs an LLM that returns structured output
reliably, cheaply and fast enough to sit inside an incident (seconds, not
minutes). The plan locks Gemini as the default provider through the official
`google-genai` SDK, behind a provider interface so the choice stays
replaceable and tests never need a network.

The plan also says: do not hard-code a model name; list the models available
to the key and pick the newest flash-class one.

## Decision

**Provider interface.** `LLMClient.generate_structured(system, user, schema,
timeout)` with three adapters: `gemini` (default), `ollama` (optional, local)
and `fake` (scripted, used by every test and by CI).

**SDK.** `google-genai` 3.2.0, async client, JSON mode with a response schema,
temperature 0.

**Model selection.** `GEMINI_MODEL` may pin a model. When it is empty the
agent selects one at first LLM contact:

1. list the models available to the key;
2. keep the plain flash-class text models (`gemini-<version>-flash`, no
   `-lite`, `-image` or `-preview` suffix) that support `generateContent`;
3. try them newest first with one short probe call and take the first that
   answers.

Step 3 exists because "listed" turned out not to mean "usable". On 2026-11-12
the listing for the project key returned 27 models, among them these
flash-class candidates:

| Model | Probe result (one or two attempts each, 2026-11-12) |
|---|---|
| `gemini-3.8-flash` | no response within 25 s, 45 s and 75 s |
| `gemini-3.7-flash` | no response within 25 s |
| `gemini-3.6-flash` | answered in 1.3–2.2 s |
| `gemini-3.5-flash` | answered in 6.2 s |

**Chosen model: `gemini-3.6-flash`**, the newest flash-class model that was
both listed and responsive. A structured-output call through the SDK returned
valid JSON matching the schema in 2.2 s (34 prompt tokens, 15 output tokens,
251 total including the model's internal reasoning tokens).

Why the two newer models did not answer was not investigated further
(possibly load or a per-key limitation at the time). If they become
responsive, the selection picks them up automatically; pin `GEMINI_MODEL` to
keep evaluation runs comparable.

**Reasoning tokens count against the output budget.** A probe with a
20-token output limit returned empty text because the budget was spent on
reasoning tokens. The adapter therefore sets a generous output limit.

## Consequences

- The model actually used is recorded on every diagnosis (`provider`, `model`,
  `prompt_version`), so results stay attributable when the selection changes.
- Probing costs a few seconds once per agent start, not per incident.
- An LLM outage, slowness or bad output never blocks an incident: per-call
  timeout, bounded retries, a circuit breaker and automatic fallback to the
  rule-based engine, with the incident flagged `degraded_mode`.
- Model output is never trusted: every proposal passes the same validator and
  policy engine as the rule-based engine's.
- The API key lives only in `.env` (gitignored) and reaches the agent as an
  environment variable. It is never logged, and prompts and responses are
  redacted before they are stored.
