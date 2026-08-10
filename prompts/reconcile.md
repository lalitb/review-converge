Reconcile the Claude and Codex reviews for {{repo}} ({{target}}), pinned at {{head_sha}}.

Snapshot source: {{source}}
Available context: {{context_availability}}

Round: {{round}}
Claude artifact: {{claude_latest}}
Codex artifact: {{codex_latest}}
Snapshot artifacts: {{snapshot_dir}}
Local checkout: {{repo_dir}}

Read both review artifacts and the pinned source. Do not accept another review merely to converge. Treat the reviewer-namespaced finding ID as its identity. For each finding, re-read the cited source and surrounding lifecycle, then accept, reject, downgrade, or mark it already covered with concrete evidence. Correct wrong lines, scope, severity, and duplicate status. Separate current correctness from future maintainability. A sibling-component bug is not automatically a blocker unless this change claims that scope or leaves the changed contract inconsistent.

Report genuinely new findings only when the other reviews missed an independent material issue. Keep stable finding IDs. Set converged=true only if there are no material blocker/high disagreements and no new substantive findings. This remains source-only: no edits, builds, tests, GitHub writes, or web access. Return only the schema-conforming result, with reviewer set to your identity.
