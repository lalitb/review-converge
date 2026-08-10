# review-converge

**Evidence-based multi-agent AI code review convergence.**

`review-converge` runs independent Claude and Codex source-only reviews, asks each reviewer to challenge the other's findings for a bounded number of rounds, and produces an evidence-based maintainer verdict.

It never posts to GitHub. Reviewers cannot edit the checkout, run builds or tests, change branches, commit, push, approve, or resolve threads.

> Alpha software: require a human maintainer to verify every final review.

## Why use it?

A second model is useful only when it independently checks the evidence. This tool keeps the first reviews independent, gives findings stable reviewer-namespaced identities, and stops early only when both reviewers agree on the verdict and every finding decision. When they do not agree, the final report records the disagreement instead of calling it consensus.

All inputs, model outputs, and the final report are retained as an auditable snapshot. See [an example final report](examples/final.md).

## Requirements

- Python 3.10+
- `git`
- authenticated `claude` and `codex` CLIs
- authenticated `gh` CLI for GitHub PR mode only

There are no Python runtime dependencies.

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

## Convergence and final decision

Initial reviews run concurrently. In each reconciliation round, both reviewers accept, reject, downgrade, or mark every namespaced finding as already covered. Early convergence requires:

- both reviewers to claim convergence;
- identical revised verdicts;
- identical decisions and corrected severities for every finding;
- no new findings; and
- no material disagreements.

After the configured maximum, the final decider resolves what it can from evidence and records anything unresolved. Three rounds is the default; `--rounds 0` skips reconciliation. Codex is the default final decider, and `--final-decider claude` is also supported.

## Outputs

Outputs are written under the system temporary directory by default. Each run contains:

- `snapshot.json`, `metadata.json`, and `diff.patch`;
- captured GitHub context, or explicit empty context in local mode;
- `round-0-*.json` independent reviews;
- one pair of JSON artifacts per reconciliation round; and
- `final.json` plus the maintainer-readable `final.md`.

## Safety and cost

- Codex runs in a read-only sandbox.
- Claude runs in plan mode with a small read-only Git command allowlist and write/web tools disabled.
- GitHub operations are reads; the tool contains no posting, approval, resolution, or push operation.
- PR content is untrusted model input. Read [SECURITY.md](SECURITY.md).
- `--claude-max-budget-usd` caps each Claude invocation where supported.
- Model providers expose different cost controls; review their CLI configuration before large reviews.
- Each reconciliation round invokes both models, followed by one final-decider invocation.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 -m review_converge --help
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution and release expectations.
