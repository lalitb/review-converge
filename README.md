# review-converge

**Evidence-based multi-agent AI code review convergence.**

`review-converge` runs exactly two independent, configurable source-only reviews, asks each reviewer to challenge the other's findings for a bounded number of rounds, and produces an evidence-based maintainer verdict. Claude, Codex, and GitHub Copilot CLI are supported reviewer transports.

It never posts to GitHub. Reviewers cannot edit the checkout, run builds or tests, change branches, commit, push, approve, or resolve threads.

> Alpha software: require a human maintainer to verify every final review.

## Why use it?

A second model is useful only when it independently checks the evidence. This tool keeps the first reviews independent, gives findings stable reviewer-namespaced identities, and stops early only when both reviewers agree on the verdict and every finding decision. When they do not agree, the final report records the disagreement instead of calling it consensus.

All inputs, model outputs, and the final report are retained as an auditable snapshot. See [an example final report](examples/final.md).

## Requirements

- Python 3.10+
- `git`
- authenticated CLIs for the two configured reviewers (`claude`, `codex`, or `copilot`)
- authenticated `gh` CLI for GitHub PR mode only

The default Claude/Codex installation has no unconditional third-party runtime dependencies. Python 3.10 installs the `tomli` compatibility package. Copilot support uses the optional `jsonschema` validator.

## Installation

From a source checkout:

```sh
python3 -m pip install -e .
review-converge --version
```

Using `pipx`:

```sh
pipx install .
```

Install Copilot support with:

```sh
pipx install '.[copilot]'
```

## GitHub PR mode

Run from any local checkout of the target repository:

```sh
review-converge --pr 1234
```

Specify the repository or output location explicitly:

```sh
review-converge \
  --repo example/project \
  --pr 1234 \
  --rounds 3 \
  --output-dir /tmp/pr-1234-review
```

PR mode reads GitHub metadata, checks, comments, reviews, and fully paginated review threads. It captures separate base-tip and merge-base SHAs, fetches pinned review refs without switching the checkout, and verifies the PR head between rounds.

Use `--no-fetch` to avoid creating or updating `refs/review-converge/*`. This still reads GitHub. Use local mode for zero GitHub access.

## Local-only mode

Review committed local changes without `gh`, GitHub authentication, network fetches, or GitHub API calls:

```sh
review-converge --local --base main
```

Choose both refs explicitly:

```sh
review-converge --local --base origin/main --head feature-branch
```

Local mode records the resolved base tip, head, and merge base, then reviews their committed diff. It creates empty GitHub-context artifacts and tells reviewers that comments, threads, reviews, and checks are unavailable.

Tracked staged and unstaged changes can be included in the pinned snapshot:

```sh
review-converge --local --base main --include-dirty
```

`--include-dirty` requires the selected `--head` to resolve to the currently checked-out `HEAD`, because staged and unstaged changes belong to that worktree. Untracked files are rejected rather than silently omitted; add, ignore, or remove them first. The run stops if the selected head or captured worktree changes.

## Dry run

Capture and inspect the exact snapshot without invoking either reviewer:

```sh
review-converge --pr 1234 --dry-run
review-converge --local --base main --dry-run
```

## Reviewers and configuration

The built-in pair is `claude:opus` (currently Opus 5) and `codex:gpt-5.6-sol` with low reasoning effort; the latter is also the default final decider. Override exactly two slots by repeating `--reviewer`, and use `--codex-reasoning-effort` to change Codex effort:

```sh
review-converge --local --base main \
  --reviewer claude:sonnet \
  --reviewer copilot:gpt-5 \
  --final-decider codex
```

Copilot always requires an explicit model and rejects `auto`. Copilot is a transport: two model families routed through one Copilot installation may share tool-layer and organization-policy behavior, so they should not be described as fully independent providers. For safety, its adapter exposes no shell; it can read the captured patch and checkout files but cannot inspect a pinned Git object that differs from the worktree.

For repeatable runs, pass a TOML file explicitly:

```sh
review-converge --config review-converge.toml --local --base main
```

See [examples/review-converge.toml](examples/review-converge.toml). Configuration is never discovered automatically from the reviewed repository because pull-request content is untrusted. Command-line values override the selected file.

## Project context

Give both reviewers immutable copies of project-specific guidance:

```sh
review-converge --local --base main \
  --context-file CONTRIBUTING.md \
  --context-file docs/architecture.md
```

Context paths must be files inside the checkout. Their copies and SHA-256 hashes are recorded, and a changed source or captured copy makes resume fail closed.

## Custom review guidance

Add trusted operator guidance inline or from a file:

```sh
review-converge --pr 1234 \
  --instruction "Review as a maintainer; prioritize compatibility and shutdown behavior" \
  --instruction-file /path/to/team-review-guidance.md
```

Both options are repeatable. Instructions are retained in `run.json`, included in resume compatibility checks, and applied to the independent reviews, reconciliation, and final decision. They may specialize the review but cannot enable checkout edits, builds, tests, network access, or GitHub writes. Only use `--instruction-file` with content you trust; repository source and `--context-file` content remain untrusted reference material rather than instructions.

## Convergence and final decision

Initial reviews run concurrently. In each reconciliation round, both reviewers accept, reject, downgrade, or mark every namespaced finding as already covered. Early convergence requires:

- both reviewers to claim convergence;
- identical revised verdicts;
- identical decisions and corrected severities for every finding;
- no new findings; and
- no material disagreements.

After the configured maximum, the final decider resolves what it can from evidence and records anything unresolved. Three rounds is the default; `--rounds 0` skips reconciliation. `codex:gpt-5.6-sol` is the default final decider; any supported `provider[:model]` can be selected.

## Resume interrupted runs

Resume an artifact directory without repeating completed paid invocations:

```sh
review-converge --resume /tmp/review-converge/pr-1234-20260810-120000
```

Resume verifies the pinned source, context, configuration, generated schemas, installed prompts, provider CLI versions, and hashes of completed artifacts. A complete run returns its existing result without invoking a model. Only `--fail-on` may be changed during resume.

## CI exit gates

Successful reviews return `0` by default, regardless of verdict. Enable a gate with `--fail-on request_changes`, `comment`, a severity (`blocker`, `high`, `medium`, or `low`), or `any-finding`.

- `0`: review completed and passed the gate
- `1`: operational, source, or validation failure
- `2`: review completed but failed the configured gate

## Outputs

Outputs are written under the system temporary directory by default. Each run contains:

- `snapshot.json`, `metadata.json`, and `diff.patch`;
- `run.json` with configuration, model, CLI, prompt, and schema provenance;
- `usage.json` with successful and failed invocation attempts, duration, tokens, cost/credits when reported, and totals;
- immutable `context/` copies and generated `schemas/`;
- captured GitHub context, or explicit empty context in local mode;
- `round-0-*.json` independent reviews;
- one pair of JSON artifacts per reconciliation round; and
- `final.json` plus the maintainer-readable `final.md`.

Long-running stages print their active reviewer slots, a heartbeat every 30 seconds, completion duration, and provider-reported token/cost fields. Unknown fields remain `unknown`. Use `--verbose` for 10-second heartbeats and details about artifacts reused during resume. Prompts and raw provider output are not streamed to the terminal.

## Safety and cost

- Codex runs in a read-only sandbox.
- Claude runs in plan mode with a small read-only Git command allowlist and write/web tools disabled.
- GitHub operations are reads; the tool contains no posting, approval, resolution, or push operation.
- PR content is untrusted model input. Read [SECURITY.md](SECURITY.md).
- `--claude-max-budget-usd` caps each Claude invocation where supported.
- `--copilot-max-ai-credits` caps each Copilot invocation where supported; Copilot CLI 1.0.79 requires a value of at least 30.
- Missing usage fields remain `null`; the tool does not invent costs from token counts.
- Each reconciliation round invokes both models, followed by one final-decider invocation.

## Public case studies

Measured public-PR examples live under `examples/case-studies/`. Each records pinned revisions, reviewer configuration, provider-reported usage, the final report, and a human assessment. They are demonstrations, not evidence that convergence universally improves review quality.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 -m review_converge --help
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution and release expectations.
