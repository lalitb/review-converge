from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import (
    AdapterInvocationError,
    ReviewerAdapter,
    authentication_status,
    make_adapter,
)
from .artifacts import (
    artifact_descriptor,
    capture_context,
    configuration_fingerprint,
    create_run_manifest,
    load_run_manifest,
    record_invocation,
    update_stage,
    verify_context,
    verify_recorded_artifacts,
)
from .config import Settings, apply_review_profile, load_settings, override_settings
from .core import ConvergeError, atomic_write_json, load_json, run, sha256_file
from .models import (
    REVIEWER_SLOTS,
    ExecutionResult,
    InvocationResult,
    Reviewer,
    Snapshot,
)
from .schema import generate_schemas

SOURCE_ROOT = Path(__file__).resolve().parent.parent
SHARE_ROOT = Path(sys.prefix) / "share" / "review-converge"
ASSET_ROOT = SOURCE_ROOT if (SOURCE_ROOT / "schemas").is_dir() else SHARE_ROOT
SCHEMAS = ASSET_ROOT / "schemas"
PROMPTS = ASSET_ROOT / "prompts"
PLACEHOLDER = re.compile(r"{{[a-z_]+}}")
FAIL_CHOICES = (
    "request_changes",
    "comment",
    "blocker",
    "high",
    "medium",
    "low",
    "any-finding",
)
SEVERITY = {"blocker": 4, "high": 3, "medium": 2, "low": 1}


def write_json(path: Path, value: Any) -> None:
    atomic_write_json(path, value)


def git_output(repo_dir: Path, args: Sequence[str], timeout: int) -> str:
    return run(["git", *args], cwd=repo_dir, timeout=timeout).stdout.strip()


def gh_json(repo_dir: Path, args: Sequence[str], timeout: int) -> Any:
    result = run(["gh", *args], cwd=repo_dir, timeout=timeout)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConvergeError(f"gh returned invalid JSON for {' '.join(args)}") from exc


def gh_paginated_array(repo_dir: Path, endpoint: str, timeout: int) -> list[Any]:
    pages = gh_json(repo_dir, ["api", endpoint, "--paginate", "--slurp"], timeout)
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise ConvergeError(f"Expected array pages from GitHub endpoint {endpoint}")
    return [item for page in pages for item in page]


def infer_repo(repo_dir: Path, timeout: int) -> str:
    return str(
        gh_json(repo_dir, ["repo", "view", "--json", "nameWithOwner"], timeout)[
            "nameWithOwner"
        ]
    )


def parse_repo(repo: str) -> tuple[str, str]:
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise ConvergeError(f"Repository must be in owner/name form: {repo}")
    return parts[0], parts[1]


def graphql(
    repo_dir: Path, query: str, variables: dict[str, str | int | None], timeout: int
) -> Any:
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is not None:
            args.extend(["-F" if isinstance(value, int) else "-f", f"{key}={value}"])
    result = gh_json(repo_dir, args, timeout)
    if not isinstance(result, dict):
        raise ConvergeError("GitHub GraphQL returned a non-object response")
    if result.get("errors"):
        messages = [
            str(error.get("message", error)) if isinstance(error, dict) else str(error)
            for error in result["errors"]
        ]
        raise ConvergeError(f"GitHub GraphQL error: {'; '.join(messages)}")
    if result.get("data") is None:
        raise ConvergeError("GitHub GraphQL response did not contain data")
    return result


def collect_review_threads(
    repo_dir: Path, repo: str, pr: int, timeout: int
) -> dict[str, Any]:
    owner, name = parse_repo(repo)
    thread_query = """query($owner:String!,$name:String!,$number:Int!,$after:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$after){nodes{id isResolved isOutdated path line originalLine comments(first:100){nodes{author{login} body createdAt url} pageInfo{hasNextPage endCursor}}} pageInfo{hasNextPage endCursor}}}}}"""
    comment_query = """query($id:ID!,$after:String){node(id:$id){... on PullRequestReviewThread{comments(first:100,after:$after){nodes{author{login} body createdAt url} pageInfo{hasNextPage endCursor}}}}}"""
    review_query = """query($owner:String!,$name:String!,$number:Int!,$after:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviews(first:100,after:$after){nodes{author{login} state body submittedAt url} pageInfo{hasNextPage endCursor}}}}}"""
    variables = {"owner": owner, "name": name, "number": pr}
    threads: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        connection = graphql(
            repo_dir, thread_query, {**variables, "after": cursor}, timeout
        )["data"]["repository"]["pullRequest"]["reviewThreads"]
        for thread in connection["nodes"]:
            comments = thread["comments"]
            while comments["pageInfo"].get("hasNextPage"):
                page = graphql(
                    repo_dir,
                    comment_query,
                    {
                        "id": thread["id"],
                        "after": comments["pageInfo"].get("endCursor"),
                    },
                    timeout,
                )["data"]["node"]["comments"]
                comments["nodes"].extend(page["nodes"])
                comments["pageInfo"] = page["pageInfo"]
            threads.append(thread)
        if not connection["pageInfo"].get("hasNextPage"):
            break
        cursor = connection["pageInfo"].get("endCursor")
    reviews: list[dict[str, Any]] = []
    cursor = None
    while True:
        connection = graphql(
            repo_dir, review_query, {**variables, "after": cursor}, timeout
        )["data"]["repository"]["pullRequest"]["reviews"]
        reviews.extend(connection["nodes"])
        if not connection["pageInfo"].get("hasNextPage"):
            break
        cursor = connection["pageInfo"].get("endCursor")
    return {"reviewThreads": threads, "reviews": reviews}


def create_output_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ConvergeError(f"Output directory already exists: {path}") from exc


def pin_fetched_base(
    repo_dir: Path, pr: int, captured_base: str, fetched_base: str, timeout: int
) -> None:
    if fetched_base == captured_base:
        return
    ancestor = run(
        ["git", "merge-base", "--is-ancestor", captured_base, fetched_base],
        cwd=repo_dir,
        timeout=timeout,
        check=False,
    )
    if ancestor.returncode:
        raise ConvergeError(
            "Captured PR base is not reachable from the fetched base branch; retry"
        )
    # GitHub's PR metadata may retain an earlier baseRefOid after the branch
    # advances. Pin the audit ref to the exact SHA used by the captured metadata.
    run(
        [
            "git",
            "update-ref",
            f"refs/review-converge/base-tip-{pr}",
            captured_base,
            fetched_base,
        ],
        cwd=repo_dir,
        timeout=timeout,
    )


def snapshot_constraints() -> dict[str, bool]:
    return {
        "source_only": True,
        "no_checkout_edits": True,
        "no_build_or_test": True,
        "no_github_writes": True,
    }


def collect_github_snapshot(
    repo_dir: Path, repo: str, pr: int, output_dir: Path, timeout: int, fetch_refs: bool
) -> Snapshot:
    parse_repo(repo)
    fields = "number,title,body,url,author,baseRefName,baseRefOid,headRefName,headRefOid,isDraft,mergeable,mergeStateStatus,reviewDecision,commits,files,statusCheckRollup"
    metadata = gh_json(
        repo_dir, ["pr", "view", str(pr), "--repo", repo, "--json", fields], timeout
    )
    if int(metadata["number"]) != pr:
        raise ConvergeError("GitHub returned metadata for a different pull request")
    head_sha, base_tip_sha = str(metadata["headRefOid"]), str(metadata["baseRefOid"])
    base_ref, head_ref = str(metadata["baseRefName"]), str(metadata["headRefName"])
    create_output_dir(output_dir)
    write_json(output_dir / "metadata.json", metadata)
    comparison = gh_json(
        repo_dir, ["api", f"repos/{repo}/compare/{base_tip_sha}...{head_sha}"], timeout
    )
    merge_base_sha = str(comparison["merge_base_commit"]["sha"])
    (output_dir / "diff.patch").write_text(
        run(
            ["gh", "pr", "diff", str(pr), "--repo", repo], cwd=repo_dir, timeout=timeout
        ).stdout,
        encoding="utf-8",
    )
    write_json(
        output_dir / "issue-comments.json",
        gh_paginated_array(
            repo_dir, f"repos/{repo}/issues/{pr}/comments?per_page=100", timeout
        ),
    )
    write_json(
        output_dir / "review-comments.json",
        gh_paginated_array(
            repo_dir, f"repos/{repo}/pulls/{pr}/comments?per_page=100", timeout
        ),
    )
    write_json(
        output_dir / "threads.json", collect_review_threads(repo_dir, repo, pr, timeout)
    )
    if fetch_refs:
        run(
            [
                "git",
                "fetch",
                "--force",
                f"https://github.com/{repo}.git",
                f"refs/pull/{pr}/head:refs/review-converge/pr-{pr}",
                f"refs/heads/{base_ref}:refs/review-converge/base-tip-{pr}",
            ],
            cwd=repo_dir,
            timeout=timeout,
        )
        fetched_head = git_output(
            repo_dir, ["rev-parse", f"refs/review-converge/pr-{pr}"], timeout
        )
        fetched_base = git_output(
            repo_dir, ["rev-parse", f"refs/review-converge/base-tip-{pr}"], timeout
        )
        if fetched_head != head_sha:
            raise ConvergeError(
                "Fetched PR head does not match captured GitHub metadata; retry"
            )
        pin_fetched_base(repo_dir, pr, base_tip_sha, fetched_base, timeout)
        local_merge_base = git_output(
            repo_dir,
            [
                "merge-base",
                f"refs/review-converge/pr-{pr}",
                f"refs/review-converge/base-tip-{pr}",
            ],
            timeout,
        )
        if local_merge_base != merge_base_sha:
            raise ConvergeError(
                "Local and GitHub merge-base calculations disagree; retry"
            )
        run(
            ["git", "update-ref", f"refs/review-converge/base-{pr}", merge_base_sha],
            cwd=repo_dir,
            timeout=timeout,
        )
    snapshot = Snapshot(
        "github",
        repo,
        f"PR #{pr}",
        pr,
        head_sha,
        merge_base_sha,
        base_tip_sha,
        base_ref,
        head_ref,
        output_dir,
    )
    write_snapshot(snapshot, repo_dir, fetch_refs)
    return snapshot


def local_diff(
    repo_dir: Path, merge_base: str, head_sha: str, include_dirty: bool, timeout: int
) -> str:
    committed = run(
        ["git", "diff", "--binary", f"{merge_base}..{head_sha}"],
        cwd=repo_dir,
        timeout=timeout,
    ).stdout
    if not include_dirty:
        return committed
    dirty = run(
        ["git", "diff", "--binary", head_sha], cwd=repo_dir, timeout=timeout
    ).stdout
    return committed + ("\n# Uncommitted tracked changes\n" + dirty if dirty else "")


def worktree_fingerprint(repo_dir: Path, head_sha: str, timeout: int) -> str:
    status = run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo_dir,
        timeout=timeout,
    ).stdout
    dirty = run(
        ["git", "diff", "--binary", head_sha], cwd=repo_dir, timeout=timeout
    ).stdout
    return hashlib.sha256((status + "\0" + dirty).encode()).hexdigest()


def collect_local_snapshot(
    repo_dir: Path,
    base_ref: str,
    head_ref: str,
    output_dir: Path,
    timeout: int,
    include_dirty: bool,
) -> Snapshot:
    head_sha = git_output(
        repo_dir, ["rev-parse", "--verify", f"{head_ref}^{{commit}}"], timeout
    )
    base_tip_sha = git_output(
        repo_dir, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"], timeout
    )
    merge_base_sha = git_output(
        repo_dir, ["merge-base", base_tip_sha, head_sha], timeout
    )
    status = run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo_dir,
        timeout=timeout,
    ).stdout
    if any(line.startswith("?? ") for line in status.splitlines()):
        raise ConvergeError(
            "Local worktree contains untracked files, which --include-dirty cannot capture; add, ignore, or remove them first"
        )
    if status and not include_dirty:
        raise ConvergeError(
            "Local worktree is dirty; commit/stash changes or use --include-dirty"
        )
    checked_out = git_output(
        repo_dir, ["rev-parse", "--verify", "HEAD^{commit}"], timeout
    )
    if include_dirty and head_sha != checked_out:
        raise ConvergeError(
            "--include-dirty requires --head to resolve to the checked-out HEAD"
        )
    fingerprint = (
        worktree_fingerprint(repo_dir, head_sha, timeout) if include_dirty else None
    )
    create_output_dir(output_dir)
    (output_dir / "diff.patch").write_text(
        local_diff(repo_dir, merge_base_sha, head_sha, include_dirty, timeout),
        encoding="utf-8",
    )
    write_json(
        output_dir / "metadata.json",
        {
            "source": "local",
            "base_ref": base_ref,
            "head_ref": head_ref,
            "dirty_worktree": bool(status),
            "include_dirty": include_dirty,
        },
    )
    write_json(output_dir / "issue-comments.json", [])
    write_json(output_dir / "review-comments.json", [])
    write_json(output_dir / "threads.json", {"reviewThreads": [], "reviews": []})
    snapshot = Snapshot(
        "local",
        repo_dir.name,
        f"local {base_ref}...{head_ref}",
        None,
        head_sha,
        merge_base_sha,
        base_tip_sha,
        base_ref,
        head_ref,
        output_dir,
        fingerprint,
    )
    write_snapshot(snapshot, repo_dir, False)
    return snapshot


def write_snapshot(snapshot: Snapshot, repo_dir: Path, fetch_refs: bool) -> None:
    write_json(
        snapshot.directory / "snapshot.json",
        {
            "source": snapshot.source,
            "repo": snapshot.repo,
            "repo_dir": str(repo_dir),
            "target": snapshot.target,
            "pr": snapshot.pr,
            "head_sha": snapshot.head_sha,
            "merge_base_sha": snapshot.merge_base_sha,
            "base_tip_sha": snapshot.base_tip_sha,
            "base_ref": snapshot.base_ref,
            "head_ref": snapshot.head_ref,
            "worktree_fingerprint": snapshot.worktree_fingerprint,
            "review_ref": f"refs/review-converge/pr-{snapshot.pr}"
            if fetch_refs
            else None,
            "base_review_ref": f"refs/review-converge/base-{snapshot.pr}"
            if fetch_refs
            else None,
            "base_tip_review_ref": f"refs/review-converge/base-tip-{snapshot.pr}"
            if fetch_refs
            else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "context_files": [],
            "context_availability": "GitHub metadata, checks, reviews, comments, and threads captured"
            if snapshot.source == "github"
            else "Local Git source only; GitHub threads, reviews, comments, and checks unavailable",
            "constraints": snapshot_constraints(),
        },
    )


def snapshot_from_json(directory: Path) -> Snapshot:
    value = load_json(directory / "snapshot.json")
    return Snapshot(
        value["source"],
        value["repo"],
        value["target"],
        value.get("pr"),
        value["head_sha"],
        value["merge_base_sha"],
        value["base_tip_sha"],
        value["base_ref"],
        value["head_ref"],
        directory,
        value.get("worktree_fingerprint"),
    )


def snapshot_unchanged(snapshot: Snapshot, repo_dir: Path, timeout: int) -> bool:
    if snapshot.source == "github":
        if snapshot.pr is None:
            raise ConvergeError("GitHub snapshot is missing its pull-request number")
        data = gh_json(
            repo_dir,
            [
                "pr",
                "view",
                str(snapshot.pr),
                "--repo",
                snapshot.repo,
                "--json",
                "headRefOid",
            ],
            timeout,
        )
        return str(data["headRefOid"]) == snapshot.head_sha
    if (
        git_output(
            repo_dir,
            ["rev-parse", "--verify", f"{snapshot.head_ref}^{{commit}}"],
            timeout,
        )
        != snapshot.head_sha
    ):
        return False
    if snapshot.worktree_fingerprint is not None:
        return (
            worktree_fingerprint(repo_dir, snapshot.head_sha, timeout)
            == snapshot.worktree_fingerprint
        )
    return not run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo_dir,
        timeout=timeout,
    ).stdout


def render_prompt(template_name: str, values: dict[str, str]) -> str:
    template = (PROMPTS / template_name).read_text(encoding="utf-8")
    expected = set(PLACEHOLDER.findall(template))
    supplied = {"{{" + key + "}}" for key in values}
    missing = sorted(expected - supplied)
    if missing:
        raise ConvergeError(
            f"Missing prompt values for {template_name}: {', '.join(missing)}"
        )
    return PLACEHOLDER.sub(
        lambda match: values[match.group(0)[2:-2]],
        template,
    )


def extract_claude_result(stdout: str) -> Any:
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ConvergeError("Claude returned invalid JSON output") from exc
    for key in ("structured_output", "result"):
        if key in envelope:
            value = envelope[key]
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except json.JSONDecodeError as exc:
                    raise ConvergeError(
                        f"Claude {key} was not valid structured JSON"
                    ) from exc
            return value
    raise ConvergeError("Claude JSON envelope has neither structured_output nor result")


def run_all(
    calls: dict[str, Callable[[], InvocationResult]],
    *,
    stage: str | None = None,
    labels: dict[str, str] | None = None,
    heartbeat_seconds: int = 30,
) -> tuple[dict[str, InvocationResult], dict[str, Exception]]:
    labels = labels or {}
    started = time.monotonic()
    if stage:
        participants = ", ".join(f"{slot}={labels.get(slot, slot)}" for slot in calls)
        print(f"{stage} started: {participants}", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = {pool.submit(call): slot for slot, call in calls.items()}
        pending = set(futures)
        completed: dict[str, InvocationResult] = {}
        failed: dict[str, Exception] = {}
        while pending:
            done, pending = concurrent.futures.wait(
                pending,
                timeout=heartbeat_seconds,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                waiting = ", ".join(sorted(futures[future] for future in pending))
                elapsed = round(time.monotonic() - started)
                print(f"{stage}: still waiting on {waiting} ({elapsed}s)", flush=True)
                continue
            for future in done:
                slot = futures[future]
                try:
                    invocation = future.result()
                    completed[slot] = invocation
                    if stage:
                        print(
                            f"{stage}: {slot} completed in "
                            f"{invocation.duration_seconds:.1f}s — "
                            f"{format_usage(invocation)}",
                            flush=True,
                        )
                except Exception as exc:  # noqa: BLE001 - preserve peer results before surfacing provider failures
                    failed[slot] = exc
                    if stage:
                        print(f"{stage}: {slot} failed", flush=True)
        return completed, failed


def format_usage(invocation: InvocationResult) -> str:
    usage = invocation.usage
    fields = (
        ("input", usage.input_tokens),
        ("cached", usage.cached_input_tokens),
        ("output", usage.output_tokens),
    )
    tokens = ", ".join(
        f"{name} {value if value is not None else 'unknown'}" for name, value in fields
    )
    extras = []
    if usage.cost_usd is not None:
        extras.append(f"reported cost ${usage.cost_usd:.6f}")
    if usage.ai_credits is not None:
        extras.append(f"AI credits {usage.ai_credits:g}")
    return "tokens: " + tokens + ((" — " + ", ".join(extras)) if extras else "")


def print_run_usage(output_dir: Path) -> None:
    totals = load_json(output_dir / "usage.json").get("totals", {})
    count = totals.get("invocation_count", 0)
    fields = (
        ("input", totals.get("input_tokens")),
        ("cached", totals.get("cached_input_tokens")),
        ("output", totals.get("output_tokens")),
    )
    tokens = ", ".join(
        f"{name} {value if value is not None else 'unknown'}" for name, value in fields
    )
    extras = []
    if totals.get("cost_usd") is not None:
        extras.append(f"reported cost ${totals['cost_usd']:.6f}")
    if totals.get("ai_credits") is not None:
        extras.append(f"AI credits {totals['ai_credits']:g}")
    suffix = (" — " + ", ".join(extras)) if extras else ""
    print(f"Run usage: {count} invocations — {tokens}{suffix}", flush=True)


def print_final_report(
    final: dict[str, Any], markdown_path: Path, *, full: bool, color: bool | None = None
) -> None:
    """Render a compact final result, plus the retained Markdown on request."""
    use_color = (
        sys.stdout.isatty() and "NO_COLOR" not in os.environ if color is None else color
    )

    def styled(value: str, code: str) -> str:
        return f"\033[{code}m{value}\033[0m" if use_color else value

    verdict = str(final.get("verdict", "unknown"))
    verdict_color = {
        "approve": "32;1",
        "comment": "33;1",
        "request_changes": "31;1",
    }.get(verdict, "1")
    findings = final.get("findings", [])
    if not isinstance(findings, list):
        findings = []
    disagreements = final.get("remaining_disagreements", [])
    if not isinstance(disagreements, list):
        disagreements = []
    convergence = "converged" if final.get("converged") else "not converged"

    print()
    print(f"Final verdict: {styled(verdict.replace('_', ' ').upper(), verdict_color)}")
    print(f"Convergence:   {convergence}")
    print(f"Findings:      {len(findings)}")
    if findings:
        print()
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            severity = str(finding.get("severity", "unknown")).upper()
            location = str(finding.get("file", "unknown"))
            line = finding.get("line")
            if isinstance(line, int):
                location += f":{line}"
            label_color = "31;1" if severity in ("BLOCKER", "HIGH") else "33;1"
            print(f"  {styled(severity.ljust(7), label_color)} {location}")
            claim = str(finding.get("claim", "")).strip()
            if claim:
                print(f"          {claim}")
    if disagreements:
        print()
        print(f"Unresolved disagreements ({len(disagreements)}):")
        for disagreement in disagreements:
            print(f"  - {disagreement}")
    print()
    print(f"Full report: {markdown_path}")
    if full:
        try:
            markdown = markdown_path.read_text(encoding="utf-8").rstrip()
        except OSError as exc:
            raise ConvergeError(
                f"Cannot read final report {markdown_path}: {exc}"
            ) from exc
        print("\n" + styled("─" * 72, "2"))
        print(markdown)


def preflight_authentication(
    adapters: dict[str, ReviewerAdapter], repo_dir: Path, timeout: int
) -> None:
    providers = list(
        dict.fromkeys(adapter.reviewer.spec.provider for adapter in adapters.values())
    )
    print("Authentication preflight:", flush=True)
    failed: list[str] = []
    for provider in providers:
        status = authentication_status(provider, repo_dir, timeout)
        if status is None:
            print(
                f"  {provider}: no non-interactive status command; checked on invocation",
                flush=True,
            )
            continue
        label = "authenticated" if status.authenticated else "not authenticated"
        print(f"  {provider}: {label} ({status.detail})", flush=True)
        if not status.authenticated:
            failed.append(provider)
    if failed:
        commands = ", ".join(
            "claude auth login" if provider == "claude" else "codex login"
            for provider in failed
        )
        raise ConvergeError(
            f"Authentication preflight failed for {', '.join(failed)}; run: {commands}"
        )


def record_invocation_failures(
    output_dir: Path,
    stage: str,
    adapters: dict[str, ReviewerAdapter],
    failed: dict[str, Exception],
) -> None:
    for slot, error in failed.items():
        if isinstance(error, AdapterInvocationError):
            adapter = adapters[slot]
            record_invocation(
                output_dir,
                stage=stage,
                reviewer=adapter.reviewer,
                cli_version=adapter.cli_version,
                result=error.result,
                outcome="failed",
                error=str(error),
            )


def raise_invocation_failures(failed: dict[str, Exception]) -> None:
    if not failed:
        return
    detail = "; ".join(f"{slot}: {error}" for slot, error in failed.items())
    raise ConvergeError(f"Reviewer invocation failed ({detail})")


def response_decisions(result: dict[str, Any]) -> dict[str, tuple[Any, Any]] | None:
    responses = result.get("responses")
    if not isinstance(responses, list):
        return None
    decisions: dict[str, tuple[Any, Any]] = {}
    for response in responses:
        finding_id = response.get("finding_id") if isinstance(response, dict) else None
        if not isinstance(finding_id, str) or finding_id in decisions:
            return None
        decision = response.get("decision")
        severity = response.get("corrected_severity")
        if decision == "downgrade":
            if severity not in SEVERITY:
                return None
        elif severity is not None:
            return None
        decisions[finding_id] = (decision, severity)
    return decisions


def finding_ids(result: dict[str, Any], field: str) -> set[str]:
    findings = result.get(field)
    if not isinstance(findings, list):
        raise ConvergeError(f"Reviewer output did not contain a {field} array")
    ids: set[str] = set()
    for finding in findings:
        finding_id = finding.get("id") if isinstance(finding, dict) else None
        if not isinstance(finding_id, str) or not finding_id or finding_id in ids:
            raise ConvergeError(
                f"Reviewer output contained an invalid or repeated {field} ID"
            )
        ids.add(finding_id)
    return ids


def validate_review(
    result: dict[str, Any], reviewer: str, field: str = "findings"
) -> set[str]:
    if result.get("reviewer") != reviewer:
        raise ConvergeError(
            f"Expected {reviewer} review identity, got {result.get('reviewer')!r}"
        )
    ids = finding_ids(result, field)
    invalid = sorted(value for value in ids if not value.startswith(f"{reviewer}:"))
    if invalid:
        raise ConvergeError(
            f"{reviewer} finding IDs must start with `{reviewer}:`: {', '.join(invalid)}"
        )
    return ids


def validate_new_findings(result: dict[str, Any], reviewer: str) -> set[str]:
    return validate_review(result, reviewer, "new_findings")


def converged(results: dict[str, dict[str, Any]], expected: set[str]) -> bool:
    values = list(results.values())
    decisions = [response_decisions(value) for value in values]
    return bool(
        all(value.get("converged") for value in values)
        and len({value.get("revised_verdict") for value in values}) == 1
        and all(
            decision is not None and set(decision) == expected for decision in decisions
        )
        and decisions[0] == decisions[1]
        and all(not value.get("material_disagreements") for value in values)
        and all(not value.get("new_findings") for value in values)
    )


def artifact_path(output_dir: Path, round_number: int, reviewer: str) -> Path:
    if reviewer not in REVIEWER_SLOTS:
        raise ConvergeError(f"Unsafe reviewer artifact identity: {reviewer}")
    return output_dir / f"round-{round_number}-{reviewer}.json"


def settings_json(settings: Settings) -> dict[str, Any]:
    return {
        "reviewers": [spec.display_name for spec in settings.reviewers],
        "final_decider": settings.final_decider.display_name,
        "rounds": settings.rounds,
        "timeout": settings.timeout,
        "context_files": list(settings.context_files),
        "instructions": list(settings.instructions),
        "review_profile": settings.review_profile,
        "fail_on": settings.fail_on,
        "claude_max_budget_usd": settings.claude_max_budget_usd,
        "copilot_max_ai_credits": settings.copilot_max_ai_credits,
        "codex_reasoning_effort": settings.codex_reasoning_effort,
        "structured_retries": settings.structured_retries,
    }


def settings_from_json(value: dict[str, Any]) -> Settings:
    return override_settings(
        Settings(),
        reviewers=value["reviewers"],
        final_decider=value["final_decider"],
        rounds=value["rounds"],
        timeout=value["timeout"],
        context_files=value["context_files"],
        instructions=value.get("instructions", []),
        review_profile=value.get("review_profile"),
        fail_on=value.get("fail_on"),
        claude_max_budget_usd=value.get("claude_max_budget_usd"),
        copilot_max_ai_credits=value.get("copilot_max_ai_credits"),
        codex_reasoning_effort=value.get("codex_reasoning_effort", "low"),
        structured_retries=value.get("structured_retries", 1),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-converge",
        description=(
            "Run exactly two independent source-only reviews, reconcile their "
            "findings, and produce an auditable maintainer verdict."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  review-converge --pr 1234
  review-converge --local --base main
  review-converge session --new /tmp/change-session --base main
  review-converge --local --base main --review-profile cheap
  review-converge --pr 1234 --instruction "Prioritize API compatibility"
  review-converge --pr 1234 --instruction-file /path/to/guidance.md
  review-converge --resume /tmp/review-converge/pr-1234-run
  review-converge --resume /tmp/review-converge/pr-1234-run --print-report

defaults:
  reviewers:     claude:opus and codex:gpt-5.6-sol
  final decider: codex:gpt-5.6-sol
  Codex effort:  low
  rounds:        3

review profiles:
  cheap:         Claude Sonnet + Codex low, no reconciliation (max 3 calls)
  balanced:      Claude Sonnet + Codex low, 1 round (max 5 calls)
  thorough:      Claude Opus + Codex medium, 3 rounds (max 9 calls)
  Profiles reduce relative effort; they are not exact cross-provider token caps.

safety:
  Reviewers cannot edit the checkout, build, test, post to GitHub, or change refs.
  Custom instructions may specialize a review but cannot override these constraints.

exit codes:
  0  review completed and passed any configured gate
  1  operational, source, or validation failure
  2  review completed but failed --fail-on
""",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pr", type=int, help="GitHub pull request number")
    mode.add_argument(
        "--local",
        action="store_true",
        help="review local Git refs without GitHub access",
    )
    mode.add_argument(
        "--resume", type=Path, help="resume a compatible artifact directory"
    )
    parser.add_argument("--config", type=Path, help="explicit TOML configuration file")
    parser.add_argument(
        "--review-profile",
        choices=("cheap", "balanced", "thorough"),
        help="cost/effort preset; explicit flags override the preset",
    )
    parser.add_argument(
        "--reviewer", action="append", help="provider[:model]; specify exactly twice"
    )
    parser.add_argument(
        "--final-decider",
        help="final provider[:model] (default: codex:gpt-5.6-sol)",
    )
    parser.add_argument("--repo", help="owner/repo; inferred from the current checkout")
    parser.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(), help="local Git checkout"
    )
    parser.add_argument("--base", help="local mode base ref")
    parser.add_argument("--head", default="HEAD", help="local mode head ref")
    parser.add_argument(
        "--include-dirty",
        action="store_true",
        help="include tracked worktree changes; rejects untracked files",
    )
    parser.add_argument(
        "--context-file",
        action="append",
        help="untrusted repository context file; repeat as needed",
    )
    parser.add_argument(
        "--instruction",
        action="append",
        help="trusted operator review guidance; repeat as needed",
    )
    parser.add_argument(
        "--instruction-file",
        action="append",
        type=Path,
        help="read trusted operator guidance from a file; repeat as needed",
    )
    parser.add_argument("--rounds", type=int, help="reconciliation rounds (default: 3)")
    parser.add_argument(
        "--output-dir", type=Path, help="new artifact directory outside the checkout"
    )
    parser.add_argument("--timeout", type=int, help="seconds allowed per provider call")
    parser.add_argument(
        "--claude-model", help="legacy default-slot Claude model override"
    )
    parser.add_argument(
        "--codex-model", help="legacy default-slot Codex model override"
    )
    parser.add_argument(
        "--claude-max-budget-usd", type=float, help="per-invocation Claude budget cap"
    )
    parser.add_argument(
        "--copilot-max-ai-credits",
        type=float,
        help="per-invocation Copilot cap (minimum: 30)",
    )
    parser.add_argument(
        "--codex-reasoning-effort",
        choices=("minimal", "low", "medium", "high", "xhigh"),
        help="Codex reasoning effort (default: low)",
    )
    parser.add_argument(
        "--structured-retries",
        type=int,
        choices=[0, 1],
        help="Copilot JSON repair attempts (default: 1)",
    )
    parser.add_argument(
        "--fail-on", choices=FAIL_CHOICES, help="return exit 2 when the gate matches"
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="do not update local review refs"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="capture inputs without invoking models"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show resume reuse details and 10-second progress heartbeats",
    )
    parser.add_argument(
        "--print-report",
        action="store_true",
        help="print the complete final Markdown report before exiting",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def resolve_settings(args: argparse.Namespace) -> Settings:
    if args.resume:
        if (
            args.config
            or args.reviewer
            or args.review_profile
            or args.final_decider
            or args.rounds is not None
            or args.context_file
            or args.instruction
            or args.instruction_file
            or args.timeout is not None
            or args.claude_model
            or args.codex_model
            or args.claude_max_budget_usd is not None
            or args.copilot_max_ai_credits is not None
            or args.codex_reasoning_effort is not None
            or args.structured_retries is not None
        ):
            raise ConvergeError(
                "--resume does not accept configuration overrides except --fail-on"
            )
        manifest = load_run_manifest(args.resume.resolve())
        settings = settings_from_json(manifest["configuration"])
        return override_settings(settings, fail_on=args.fail_on)
    settings = load_settings(args.config.resolve() if args.config else None)
    if args.review_profile:
        settings = apply_review_profile(settings, args.review_profile)
    instructions = list(args.instruction or [])
    for path in args.instruction_file or []:
        try:
            instructions.append(path.resolve().read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConvergeError(f"Cannot read instruction file {path}: {exc}") from exc
    reviewers = args.reviewer
    if reviewers and (args.claude_model or args.codex_model):
        raise ConvergeError("--reviewer cannot be combined with legacy model flags")
    if not reviewers and (args.claude_model or args.codex_model):
        configured = settings.reviewers
        reviewers = [
            f"claude:{args.claude_model}"
            if args.claude_model
            else configured[0].display_name,
            f"codex:{args.codex_model}"
            if args.codex_model
            else configured[1].display_name,
        ]
    return override_settings(
        settings,
        reviewers=reviewers,
        final_decider=args.final_decider,
        rounds=args.rounds,
        timeout=args.timeout,
        context_files=args.context_file,
        instructions=instructions or None,
        fail_on=args.fail_on,
        claude_max_budget_usd=args.claude_max_budget_usd,
        copilot_max_ai_credits=args.copilot_max_ai_credits,
        codex_reasoning_effort=args.codex_reasoning_effort,
        structured_retries=args.structured_retries,
    )


def validate_args(args: argparse.Namespace, settings: Settings) -> None:
    if args.resume:
        forbidden = (
            args.pr
            or args.local
            or args.repo
            or args.base
            or args.include_dirty
            or args.no_fetch
            or args.output_dir
            or args.dry_run
        )
        if forbidden:
            raise ConvergeError(
                "--resume cannot be combined with source or output options"
            )
        return
    if args.local and not args.base:
        raise ConvergeError("--local requires --base")
    if not args.local and (args.base or args.head != "HEAD" or args.include_dirty):
        raise ConvergeError("--base, --head, and --include-dirty require --local")
    if args.local and (args.repo or args.no_fetch):
        raise ConvergeError("--repo and --no-fetch are GitHub-mode options")


def build_reviewers(settings: Settings) -> tuple[list[Reviewer], Reviewer]:
    reviewers = [
        Reviewer(slot, spec) for slot, spec in zip(REVIEWER_SLOTS, settings.reviewers)
    ]
    matching = next(
        (reviewer for reviewer in reviewers if reviewer.spec == settings.final_decider),
        None,
    )
    return reviewers, matching or Reviewer("decider", settings.final_decider)


def common_values(snapshot: Snapshot, repo_dir: Path) -> dict[str, str]:
    data = load_json(snapshot.directory / "snapshot.json")
    context = [
        str(snapshot.directory / item["artifact"])
        for item in data.get("context_files", [])
    ]
    return {
        "repo": snapshot.repo,
        "target": snapshot.target,
        "source": snapshot.source,
        "head_sha": snapshot.head_sha,
        "base_sha": snapshot.merge_base_sha,
        "snapshot_dir": str(snapshot.directory),
        "repo_dir": str(repo_dir),
        "context_availability": data["context_availability"],
        "context_artifacts": json.dumps(context),
    }


def invoke_and_store(
    adapter: ReviewerAdapter,
    prompt: str,
    schema: Path,
    artifact: Path,
    output_dir: Path,
    stage: str,
) -> dict[str, Any]:
    result = adapter.invoke(prompt, schema)
    write_json(artifact, result.value)
    record_invocation(
        output_dir,
        stage=stage,
        reviewer=adapter.reviewer,
        cli_version=adapter.cli_version,
        result=result,
    )
    return result.value


def ensure_unchanged(snapshot: Snapshot, repo_dir: Path, timeout: int) -> None:
    if not snapshot_unchanged(snapshot, repo_dir, timeout):
        raise ConvergeError(
            "Review source changed since the snapshot; artifacts retained"
        )
    data = load_json(snapshot.directory / "snapshot.json")
    verify_context(repo_dir, snapshot.directory, data.get("context_files", []))


def gate_failed(result: ExecutionResult, fail_on: str | None) -> bool:
    if not fail_on:
        return False
    if fail_on == "request_changes":
        return result.verdict == "request_changes"
    if fail_on == "comment":
        return result.verdict in ("comment", "request_changes")
    if fail_on == "any-finding":
        return bool(result.final_findings)
    threshold = SEVERITY[fail_on]
    return any(
        SEVERITY.get(str(finding.get("severity")), 0) >= threshold
        for finding in result.final_findings
    )


def prepare_new_run(
    args: argparse.Namespace, settings: Settings, repo_dir: Path
) -> tuple[
    Snapshot, dict[str, Path], list[Reviewer], Reviewer, dict[str, ReviewerAdapter]
]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    label = "local" if args.local else f"pr-{args.pr}"
    output_dir = (
        args.output_dir
        or Path(tempfile.gettempdir()) / "review-converge" / f"{label}-{stamp}"
    ).resolve()
    if output_dir.is_relative_to(repo_dir):
        raise ConvergeError("Output directory must be outside the reviewed checkout")
    snapshot = (
        collect_local_snapshot(
            repo_dir,
            args.base,
            args.head,
            output_dir,
            settings.timeout,
            args.include_dirty,
        )
        if args.local
        else collect_github_snapshot(
            repo_dir,
            args.repo or infer_repo(repo_dir, settings.timeout),
            args.pr,
            output_dir,
            settings.timeout,
            not args.no_fetch,
        )
    )
    captured = capture_context(repo_dir, output_dir, settings.context_files)
    snapshot_data = load_json(output_dir / "snapshot.json")
    snapshot_data["context_files"] = captured
    write_json(output_dir / "snapshot.json", snapshot_data)
    reviewers, final_decider = build_reviewers(settings)
    schemas = generate_schemas(SCHEMAS, output_dir, list(REVIEWER_SLOTS))
    if args.dry_run:
        create_run_manifest(
            output_dir,
            config=settings_json(settings),
            reviewers=reviewers,
            final_decider=final_decider,
            adapter_versions={
                slot: "not-invoked" for slot in (*REVIEWER_SLOTS, "decider")
            },
            prompt_hashes={
                name: sha256_file(PROMPTS / f"{name}.md")
                for name in ("review", "reconcile", "final")
            },
            schema_hashes={name: sha256_file(path) for name, path in schemas.items()},
        )
        return snapshot, schemas, reviewers, final_decider, {}
    unique = {reviewer.slot: reviewer for reviewer in [*reviewers, final_decider]}
    adapters = {
        slot: make_adapter(
            reviewer,
            repo_dir,
            settings.timeout,
            accessible_dirs=(output_dir,),
            claude_max_budget_usd=settings.claude_max_budget_usd,
            copilot_max_ai_credits=settings.copilot_max_ai_credits,
            codex_reasoning_effort=settings.codex_reasoning_effort,
            structured_retries=settings.structured_retries,
        )
        for slot, reviewer in unique.items()
    }
    create_run_manifest(
        output_dir,
        config=settings_json(settings),
        reviewers=reviewers,
        final_decider=final_decider,
        adapter_versions={
            slot: adapter.cli_version for slot, adapter in adapters.items()
        },
        prompt_hashes={
            name: sha256_file(PROMPTS / f"{name}.md")
            for name in ("review", "reconcile", "final")
        },
        schema_hashes={name: sha256_file(path) for name, path in schemas.items()},
    )
    return snapshot, schemas, reviewers, final_decider, adapters


def prepare_resume(
    args: argparse.Namespace, settings: Settings, repo_dir: Path
) -> tuple[
    Snapshot, dict[str, Path], list[Reviewer], Reviewer, dict[str, ReviewerAdapter]
]:
    output_dir = args.resume.resolve()
    manifest = load_run_manifest(output_dir)
    verify_recorded_artifacts(output_dir, manifest)
    comparable = settings_json(settings)
    comparable["fail_on"] = manifest["configuration"].get("fail_on")
    if configuration_fingerprint(comparable) != manifest["configuration_sha256"]:
        raise ConvergeError("Resume configuration does not match the original run")
    snapshot = snapshot_from_json(output_dir)
    if Path(load_json(output_dir / "snapshot.json")["repo_dir"]).resolve() != repo_dir:
        raise ConvergeError("Resume must use the original reviewed checkout")
    schemas = {
        name: output_dir / "schemas" / f"{name}.json"
        for name in ("review", "reconciliation", "final")
    }
    for name, path in schemas.items():
        if sha256_file(path) != manifest["schema_sha256"][name]:
            raise ConvergeError(f"Resume schema changed: {name}")
    for name in ("review", "reconcile", "final"):
        if sha256_file(PROMPTS / f"{name}.md") != manifest["prompt_sha256"][name]:
            raise ConvergeError(f"Installed prompt changed since run: {name}")
    reviewers, final_decider = build_reviewers(settings)
    ensure_unchanged(snapshot, repo_dir, settings.timeout)
    if (output_dir / "final.json").is_file():
        return snapshot, schemas, reviewers, final_decider, {}
    unique = {reviewer.slot: reviewer for reviewer in [*reviewers, final_decider]}
    adapters = {
        slot: make_adapter(
            reviewer,
            repo_dir,
            settings.timeout,
            accessible_dirs=(output_dir,),
            claude_max_budget_usd=settings.claude_max_budget_usd,
            copilot_max_ai_credits=settings.copilot_max_ai_credits,
            codex_reasoning_effort=settings.codex_reasoning_effort,
            structured_retries=settings.structured_retries,
        )
        for slot, reviewer in unique.items()
    }
    expected_versions = {
        item["slot"]: item["cli_version"] for item in manifest["reviewers"]
    }
    expected_versions[manifest["final_decider"]["slot"]] = manifest["final_decider"][
        "cli_version"
    ]
    for slot, adapter in adapters.items():
        if expected_versions.get(slot) != adapter.cli_version:
            raise ConvergeError(f"Provider CLI version changed for {slot}")
    return snapshot, schemas, reviewers, final_decider, adapters


def execute(args: argparse.Namespace) -> ExecutionResult:
    settings = resolve_settings(args)
    validate_args(args, settings)
    repo_dir = args.repo_dir.resolve()
    if (
        git_output(repo_dir, ["rev-parse", "--is-inside-work-tree"], settings.timeout)
        != "true"
    ):
        raise ConvergeError(f"Not a Git worktree: {repo_dir}")
    snapshot, schemas, reviewers, final_decider, adapters = (
        prepare_resume(args, settings, repo_dir)
        if args.resume
        else prepare_new_run(args, settings, repo_dir)
    )
    output_dir = snapshot.directory
    print(f"Snapshot: {output_dir}\nHead:     {snapshot.head_sha}")
    if args.dry_run:
        print("Dry run complete; no model was invoked.")
        return ExecutionResult(output_dir, None)
    final_path = output_dir / "final.json"
    if final_path.is_file():
        final = load_json(final_path)
        print("Run already complete; reusing existing final review.", flush=True)
        print_run_usage(output_dir)
        print_final_report(final, output_dir / "final.md", full=args.print_report)
        return ExecutionResult(
            output_dir, final["verdict"], tuple(final.get("findings", []))
        )
    preflight_authentication(adapters, repo_dir, settings.timeout)
    common = common_values(snapshot, repo_dir)
    common["custom_instructions"] = (
        "\n".join(
            f"{index}. {instruction.strip()}"
            for index, instruction in enumerate(settings.instructions, 1)
        )
        if settings.instructions
        else "None supplied."
    )
    initial: dict[str, dict[str, Any]] = {}
    calls: dict[str, Callable[[], InvocationResult]] = {}
    labels = {reviewer.slot: reviewer.spec.display_name for reviewer in reviewers}
    heartbeat = 10 if args.verbose else 30
    for reviewer in reviewers:
        path = artifact_path(output_dir, 0, reviewer.slot)
        if path.is_file():
            initial[reviewer.slot] = load_json(path)
            if args.verbose:
                print(f"Initial reviews: reusing {path.name}", flush=True)
        else:
            prompt = render_prompt(
                "review.md",
                {
                    **common,
                    "reviewer_id": reviewer.slot,
                    "reviewer_label": reviewer.spec.display_name,
                },
            )
            calls[reviewer.slot] = lambda a=adapters[reviewer.slot], p=prompt: a.invoke(
                p, schemas["review"]
            )
    completed, failed = (
        run_all(
            calls,
            stage="Initial reviews",
            labels=labels,
            heartbeat_seconds=heartbeat,
        )
        if calls
        else ({}, {})
    )
    for slot, invocation in completed.items():
        path = artifact_path(output_dir, 0, slot)
        write_json(path, invocation.value)
        record_invocation(
            output_dir,
            stage="initial",
            reviewer=adapters[slot].reviewer,
            cli_version=adapters[slot].cli_version,
            result=invocation,
        )
        update_stage(
            output_dir, ("initial", slot), artifact_descriptor(output_dir, path)
        )
        initial[slot] = invocation.value
    record_invocation_failures(output_dir, "initial", adapters, failed)
    raise_invocation_failures(failed)
    expected: set[str] = set()
    for reviewer in reviewers:
        expected |= validate_review(initial[reviewer.slot], reviewer.slot)
    update_stage(
        output_dir,
        ("initial",),
        {
            slot: artifact_descriptor(output_dir, artifact_path(output_dir, 0, slot))
            for slot in REVIEWER_SLOTS
        },
    )
    rounds_run = 0
    for round_number in range(1, settings.rounds + 1):
        ensure_unchanged(snapshot, repo_dir, settings.timeout)
        artifact_listing = json.dumps(
            [
                {
                    "reviewer": reviewer.slot,
                    "path": str(
                        artifact_path(output_dir, round_number - 1, reviewer.slot)
                    ),
                }
                for reviewer in reviewers
            ]
        )
        values = {
            **common,
            "round": str(round_number),
            "reviewer_artifacts": artifact_listing,
            "artifact_glob": str(output_dir / "round-*.json"),
            "expected_finding_ids": json.dumps(sorted(expected)),
        }
        current: dict[str, dict[str, Any]] = {}
        calls = {}
        for reviewer in reviewers:
            path = artifact_path(output_dir, round_number, reviewer.slot)
            if path.is_file():
                current[reviewer.slot] = load_json(path)
                if args.verbose:
                    print(
                        f"Reconciliation round {round_number}: reusing {path.name}",
                        flush=True,
                    )
            else:
                prompt = render_prompt(
                    "reconcile.md",
                    {
                        **values,
                        "reviewer_id": reviewer.slot,
                        "reviewer_label": reviewer.spec.display_name,
                    },
                )
                calls[reviewer.slot] = lambda a=adapters[reviewer.slot], p=prompt: (
                    a.invoke(p, schemas["reconciliation"])
                )
        completed, failed = (
            run_all(
                calls,
                stage=f"Reconciliation round {round_number}",
                labels=labels,
                heartbeat_seconds=heartbeat,
            )
            if calls
            else ({}, {})
        )
        for slot, invocation in completed.items():
            path = artifact_path(output_dir, round_number, slot)
            write_json(path, invocation.value)
            record_invocation(
                output_dir,
                stage=f"round-{round_number}",
                reviewer=adapters[slot].reviewer,
                cli_version=adapters[slot].cli_version,
                result=invocation,
            )
            update_stage(
                output_dir,
                ("rounds", str(round_number), slot),
                artifact_descriptor(output_dir, path),
            )
            current[slot] = invocation.value
        record_invocation_failures(
            output_dir, f"round-{round_number}", adapters, failed
        )
        raise_invocation_failures(failed)
        new_ids: set[str] = set()
        for reviewer in reviewers:
            new_ids |= validate_new_findings(current[reviewer.slot], reviewer.slot)
        if expected & new_ids:
            raise ConvergeError("Reconciliation reused an existing finding ID")
        rounds_run = round_number
        update_stage(
            output_dir,
            ("rounds", str(round_number)),
            {
                slot: artifact_descriptor(
                    output_dir, artifact_path(output_dir, round_number, slot)
                )
                for slot in REVIEWER_SLOTS
            },
        )
        if converged(current, expected):
            print(f"Converged after round {round_number}.")
            break
        expected |= new_ids
    ensure_unchanged(snapshot, repo_dir, settings.timeout)
    artifact_listing = json.dumps(
        [
            {
                "reviewer": reviewer.slot,
                "path": str(artifact_path(output_dir, rounds_run, reviewer.slot)),
            }
            for reviewer in reviewers
        ]
    )
    final_prompt = render_prompt(
        "final.md",
        {
            **common,
            "final_decider": final_decider.spec.display_name,
            "rounds_run": str(rounds_run),
            "reviewer_artifacts": artifact_listing,
            "artifact_glob": str(output_dir / "round-*.json"),
        },
    )
    final_completed, final_failed = run_all(
        {
            final_decider.slot: lambda: adapters[final_decider.slot].invoke(
                final_prompt, schemas["final"]
            )
        },
        stage="Final decision",
        labels={final_decider.slot: final_decider.spec.display_name},
        heartbeat_seconds=heartbeat,
    )
    if final_failed:
        exc = final_failed[final_decider.slot]
        if isinstance(exc, AdapterInvocationError):
            adapter = adapters[final_decider.slot]
            record_invocation(
                output_dir,
                stage="final",
                reviewer=adapter.reviewer,
                cli_version=adapter.cli_version,
                result=exc.result,
                outcome="failed",
                error=str(exc),
            )
        raise_invocation_failures(final_failed)
    final_invocation = final_completed[final_decider.slot]
    write_json(final_path, final_invocation.value)
    record_invocation(
        output_dir,
        stage="final",
        reviewer=adapters[final_decider.slot].reviewer,
        cli_version=adapters[final_decider.slot].cli_version,
        result=final_invocation,
    )
    markdown = final_invocation.value.get("final_markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise ConvergeError("Final output did not contain final_markdown")
    (output_dir / "final.md").write_text(markdown.rstrip() + "\n", encoding="utf-8")
    update_stage(output_dir, ("final",), artifact_descriptor(output_dir, final_path))
    print_run_usage(output_dir)
    print_final_report(
        final_invocation.value,
        output_dir / "final.md",
        full=args.print_report,
    )
    return ExecutionResult(
        output_dir,
        final_invocation.value["verdict"],
        tuple(final_invocation.value.get("findings", [])),
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if arguments and arguments[0] == "session":
        from .session import session_main

        return session_main(arguments[1:])
    parser = build_parser()
    args = parser.parse_args(arguments)
    try:
        result = execute(args)
        settings = resolve_settings(args)
        return 2 if gate_failed(result, settings.fail_on) else 0
    except ConvergeError as exc:
        print(f"review-converge: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("review-converge: interrupted", file=sys.stderr)
        return 130
