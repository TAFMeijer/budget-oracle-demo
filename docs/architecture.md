# Architecture notes

## Why the model never talks to the database directly

The language model produces text. The only thing that reaches the database is the output of
`bqa.guard.validate`, which parses that text into an AST, checks it, and **renders the AST
back to SQL in the target dialect**. Two consequences:

- Comment tricks, stacked statements and dialect quirks in the model's text do not survive
  the round-trip: if sqlglot cannot parse it as a single SELECT, it does not run.
- The row limit is applied on the AST (`LIMIT n` / `TOP n`), not by string concatenation.

## Why valid values are in the prompt

Most wrong answers in NL→SQL are not syntax errors; they are *plausible filters on values
that do not exist* (`WHERE module = 'HIV prevention'` when the module is called
"Prevention programs for key populations"). Listing the distinct values of every
low-cardinality text column (`VALID_VALUES_MAX`, default 80) lets the model map the user's
wording to a value that exists — and lets the guard check that it did. For very wide tables,
restrict `ALLOWED_TABLES` or lower the threshold.

## The three-outcome contract

The model must answer with a JSON object whose `status` is `sql`, `clarify` or `cannot`. A
`sql` answer may add a `note`: one sentence naming an assumption or mapping the user might not
expect, shown with the result and replayed in the conversation history so the model remembers
what it assumed. A `clarify` answer may add `options`: two to five answers the user can pick
from, with `multi` saying whether they can be combined. The app shows them under the question
as a pick-one list (options that contradict each other) or tick-boxes with a Send button
(options that can be combined); the pick, or a typed number ("2", or "1, 3"), is replaced by
the option text before it goes back to the model as the clarification, and the pick is what the
transcript shows. `bqa.llm.parse_decision` is tolerant (code fences, the legacy `CLARIFICATION_NEEDED:` /
`CANNOT_ANSWER` prefixes, bare SQL), but anything unrecognisable is treated as `cannot`
— the assistant fails closed.

## Choosing a local model

The default is **Qwen3.6-27B** (`qwen3.6:27b`, ~17 GB at Q4_K_M): a dense, Apache-2.0
hybrid-thinking model whose instruction following is strong enough to hold the
JSON-only contract reliably. Thinking is switched off in the Ollama call (`"think": false`)
— chain-of-thought adds latency and the parser strips a `<think>` block anyway if one
slips through.

On smaller machines, SQL-tuned coder models (`qwen2.5-coder:7b`, `codellama`, `sqlcoder`)
are markedly better at exact value matching than general chat models of the same size.
Temperature is fixed at 0 in all cases.

Who sees the schema: only the model provider configured here. The introspected schema and
the valid-value lists are embedded in the system prompt of every request — local by
default; with `LLM_PROVIDER=openai` they are sent to that endpoint.

## Production checklist

- Read-only database login; network path from the app host to the database only.
- `ALLOWED_TABLES` limited to curated views rather than raw tables where possible.
- Rotate the audit log (`logs/audit.jsonl`) and review `rejected` / `cannot` entries — they
  are the cheapest source of prompt and schema improvements.
- Keep `ENABLE_OBSERVATIONS` off if result tables may contain sensitive rows you do not want
  sent to the model a second time (relevant when the provider is remote).
