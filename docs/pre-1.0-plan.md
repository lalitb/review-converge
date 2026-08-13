# Pre-1.0 implementation plan

Status: implemented and verified
Updated: 2026-08-10

Verification completed with 41 unit/integration tests, Ruff, Markdown lint, an installed-wheel dry run, two measured public-PR cases, and a live shell-free Copilot adapter smoke test.

## Objective

Turn `review-converge` into a production-quality, auditable pre-1.0 CLI while preserving its defining constraints:

- exactly two independent active reviewers;
- evidence-based reconciliation with honest disagreement;
- no checkout edits, builds, tests, GitHub writes, or implicit network access by reviewers;
- immutable, resumable run artifacts;
- explicit provider, model, cost, and validation limits;
- a human remains responsible for the final merge decision.

N-way convergence, GitHub posting, and a web UI are intentionally out of scope.

## Architecture

The implementation will be split into small modules with one-way dependencies:

```text
CLI/config
    -> run orchestration
        -> snapshots and immutable context
        -> reviewer adapters
        -> dynamic schemas and validation
        -> artifact manifest and usage ledger
        -> convergence and verdict gates
```

Stable reviewer slots are `r1` and `r2`. A slot maps to a provider/model specification in the run manifest. Artifact paths and finding IDs use the slot, never an arbitrary model name:

```text
round-0-r1.json
r1:F1
```

This makes paths safe and keeps identities stable if provider model names contain `/`, `:`, or spaces.

## Configuration contract

CLI flags override an explicitly selected TOML file, which overrides built-in defaults. Repository configuration is never loaded implicitly because a pull request can modify repository files.

```toml
reviewers = ["claude:sonnet", "copilot:gemini-3.1-pro-preview"]
final_decider = "codex"
rounds = 3
context_files = ["CONTRIBUTING.md", "docs/architecture.md"]
fail_on = "request_changes"
```

Exactly two reviewers are required. Copilot specifications require an explicit non-`auto` model. The final decider may be an active reviewer provider or a separate provider/model specification.

## Artifact contract

Each run directory contains:

- `snapshot.json`: immutable source identity, context hashes, and source constraints;
- `run.json`: format version, CLI version, configuration hash, prompt/schema hashes, provider/model/CLI provenance, stage status, and resume compatibility data;
- `usage.json`: per-attempt duration, tokens, provider-reported cost or credits, and totals with unknown values preserved as `null`;
- `context/`: immutable copies of explicitly selected context files;
- existing source, review, reconciliation, and final artifacts.

Manifest and usage writes use atomic replacement. Raw provider output is retained before semantic validation where safe.

## Resume invariants

`--resume DIR` continues only when all of the following still match:

- artifact format and CLI compatibility;
- repository and source mode;
- pinned head, merge base, and dirty-worktree fingerprint;
- complete context-file content hashes;
- reviewer and final-decider specifications;
- rounds, prompts, and generated-schema hashes;
- completed artifact schema and reviewer identities.

A partially completed reviewer pair resumes only the missing invocation. A completed final artifact returns without invoking a provider. Any incompatibility fails closed with an exact explanation.

## Provider adapters

All adapters return a common invocation result containing structured output, raw provider metadata, usage, duration, requested/resolved model, CLI version, and attempt count.

- Claude: native JSON Schema, read-only plan permissions, optional USD budget.
- Codex: native output schema, read-only sandbox, JSONL usage events.
- Copilot: explicit model, JSONL output, no questions, no custom instructions, no builtin MCP, only read plus narrowly filtered Git inspection commands. Local JSON Schema validation is provided by the optional `copilot` package extra. One repair attempt is the maximum and is separately accounted.

Provider/model resolution and CLI versions are captured before the first paid invocation.

## Verdict gates

Default successful execution remains exit code `0`. `--fail-on` enables CI gating:

- exit `0`: completed review passes the configured gate;
- exit `1`: operational or validation failure;
- exit `2`: completed review fails the configured verdict/severity gate.

The final schema will expose structured final findings so severity gates do not scrape Markdown.

## Delivery slices

### Slice 1: foundations

- Extract models, schemas, adapters, configuration, artifacts, and orchestration modules.
- Preserve the current default Claude + Codex behavior.
- Generate reviewer enums dynamically for `r1` and `r2`.
- Replace named Claude/Codex orchestration variables with a two-slot collection.

Verification: existing tests remain green; default prompts and artifact sequence remain behaviorally equivalent.

### Slice 2: auditability and context

- Capture run provenance and usage.
- Add explicit `--context-file` with immutable copies and hashes.
- Record prompt/schema/config hashes.
- Document unknown provider usage fields honestly.

Verification: mocked provider metadata tests, context mutation tests, installed-wheel asset tests.

### Slice 3: resume and CI gates

- Implement stage-aware `--resume`.
- Validate every resume invariant.
- Add structured final findings and `--fail-on`.
- Preserve distinct operational and review-gate exit codes.

Verification: resume after every stage, partial pair recovery, mismatch rejection, final no-op resume, gate matrix.

### Slice 4: configurable reviewers and Copilot

- Add repeatable `--reviewer` with exactly two values.
- Add configurable final decider.
- Add safe Copilot adapter and optional schema-validation extra.
- Add explicit TOML configuration.

Verification: model-name/path safety, precedence matrix, missing extra, `auto` rejection, permission argv, malformed output and bounded repair.

### Slice 5: evidence and release

- Run two or three bounded reviews of small public PRs with known outcomes.
- Commit concise case studies containing pinned revisions, configuration, final report, usage/cost, confirmed findings, false positives, and limitations.
- Update README, security model, contributing guide, examples, and CI.

Verification: Python 3.10-3.13 tests, wheel build/install, local and GitHub dry runs, source audit for write-capable provider/GitHub commands, clean repository state.

## Acceptance criteria

- Default invocation remains simple and uses `claude:opus` (currently Opus 5) plus `codex:gpt-5.6-sol` at low reasoning effort, with the Codex model as final decider.
- Exactly two active reviewers are enforced everywhere.
- Copilot cannot select `auto` and cannot receive write, URL, memory, or GitHub MCP tools.
- Every final claim can be traced to immutable source, context, provider, model, prompt, schema, and usage artifacts.
- Interrupted work resumes without repeating completed paid invocations.
- CI can distinguish an operational failure from a completed negative verdict.
- No feature posts to GitHub or mutates the reviewed checkout.
- Public examples make no unsupported quality-improvement claim.
