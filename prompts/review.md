You are performing an independent principal-maintainer review of {{repo}} ({{target}}).

Snapshot source: {{source}}

Pinned head: {{head_sha}}
Pinned base: {{base_sha}}
Local checkout: {{repo_dir}}
Snapshot artifacts: {{snapshot_dir}}

Read snapshot.json, metadata.json, diff.patch, threads.json, review-comments.json, and issue-comments.json. If review refs are present, use git show/diff on those refs to inspect surrounding source and call paths. Do not fetch newer state yourself.

Available context: {{context_availability}}. Do not infer GitHub review state when it is unavailable.

This is source-only. Do not edit or switch the checkout, build, test, commit, push, approve, resolve threads, or post to GitHub. Do not use the web. Review architecture and behavioral contracts first, then lifecycle, ownership, concurrency, shutdown, API/ABI compatibility, hot-path cost, tests, CI wiring, and integration overlap.

Only report findings introduced by this PR. Distinguish blockers, non-blocking comments, scope follow-ups, and concerns already covered by an existing thread. Do not invent findings. "No code findings" is valid.

Every finding must use a reviewer-namespaced stable ID (`claude:F1` or `codex:F1`), current-head file and line, short snippet, concrete evidence and impact, and a short easy-English paste-ready comment. Mark duplicate/resolved/outdated threads accurately when thread context is available. Return only the schema-conforming result.
