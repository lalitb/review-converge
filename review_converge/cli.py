from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

from . import __version__


SOURCE_ROOT = Path(__file__).resolve().parent.parent
SHARE_ROOT = Path(sys.prefix) / "share" / "review-converge"
ASSET_ROOT = SOURCE_ROOT if (SOURCE_ROOT / "schemas").is_dir() else SHARE_ROOT
SCHEMAS = ASSET_ROOT / "schemas"
PROMPTS = ASSET_ROOT / "prompts"
PLACEHOLDER = re.compile(r"{{[a-z_]+}}")


class ConvergeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    source: str
    repo: str
    target: str
    pr: int | None
    head_sha: str
    merge_base_sha: str
    base_tip_sha: str
    base_ref: str
    head_ref: str
    directory: Path
    worktree_fingerprint: str | None = None


def run(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    stdin: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            input=stdin,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ConvergeError(f"Required command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConvergeError(f"Command timed out after {timeout}s: {shlex.join(argv)}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ConvergeError(f"Command failed ({result.returncode}): {shlex.join(argv)}\n{detail}")
    return result


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConvergeError(f"Invalid JSON artifact {path}: {exc}") from exc


def git_output(repo_dir: Path, args: Sequence[str], timeout: int) -> str:
    return run(["git", *args], cwd=repo_dir, timeout=timeout).stdout.strip()


def gh_json(repo_dir: Path, args: Sequence[str], timeout: int) -> Any:
    result = run(["gh", *args], cwd=repo_dir, timeout=timeout)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConvergeError(f"gh returned invalid JSON for {shlex.join(args)}") from exc


def gh_paginated_array(repo_dir: Path, endpoint: str, timeout: int) -> list[Any]:
    pages = gh_json(repo_dir, ["api", endpoint, "--paginate", "--slurp"], timeout)
    if not isinstance(pages, list):
        raise ConvergeError(f"Expected an array of pages from GitHub endpoint {endpoint}")
    flattened: list[Any] = []
    for page in pages:
        if not isinstance(page, list):
            raise ConvergeError(f"Expected an array page from GitHub endpoint {endpoint}")
        flattened.extend(page)
    return flattened


def infer_repo(repo_dir: Path, timeout: int) -> str:
    data = gh_json(repo_dir, ["repo", "view", "--json", "nameWithOwner"], timeout)
    return str(data["nameWithOwner"])


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
        if value is None:
            continue
        flag = "-F" if isinstance(value, int) else "-f"
        args.extend([flag, f"{key}={value}"])
    result = gh_json(repo_dir, args, timeout)
    if not isinstance(result, dict):
        raise ConvergeError("GitHub GraphQL returned a non-object response")
    errors = result.get("errors")
    if errors:
        messages = [
            str(error.get("message", error)) if isinstance(error, dict) else str(error)
            for error in errors
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

    threads: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        data = graphql(
            repo_dir,
            thread_query,
            {"owner": owner, "name": name, "number": pr, "after": cursor},
            timeout,
        )
        connection = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        for thread in connection["nodes"]:
            comments = thread["comments"]
            comment_cursor = comments["pageInfo"].get("endCursor") or ""
            while comments["pageInfo"].get("hasNextPage"):
                page = graphql(
                    repo_dir,
                    comment_query,
                    {"id": thread["id"], "after": comment_cursor or None},
                    timeout,
                )["data"]["node"]["comments"]
                comments["nodes"].extend(page["nodes"])
                comments["pageInfo"] = page["pageInfo"]
                comment_cursor = page["pageInfo"].get("endCursor") or ""
            threads.append(thread)
        if not connection["pageInfo"].get("hasNextPage"):
            break
        cursor = connection["pageInfo"].get("endCursor") or ""

    reviews: list[dict[str, Any]] = []
    cursor = None
    while True:
        data = graphql(
            repo_dir,
            review_query,
            {"owner": owner, "name": name, "number": pr, "after": cursor},
            timeout,
        )
        connection = data["data"]["repository"]["pullRequest"]["reviews"]
        reviews.extend(connection["nodes"])
        if not connection["pageInfo"].get("hasNextPage"):
            break
        cursor = connection["pageInfo"].get("endCursor") or ""
    return {"reviewThreads": threads, "reviews": reviews}


def create_output_dir(output_dir: Path) -> None:
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ConvergeError(f"Output directory already exists: {output_dir}") from exc


def collect_github_snapshot(
    repo_dir: Path,
    repo: str,
    pr: int,
    output_dir: Path,
    timeout: int,
    fetch_refs: bool,
) -> Snapshot:
    parse_repo(repo)
    fields = (
        "number,title,body,url,author,baseRefName,baseRefOid,headRefName,headRefOid,"
        "isDraft,mergeable,mergeStateStatus,reviewDecision,commits,files,statusCheckRollup"
    )
    metadata = gh_json(
        repo_dir, ["pr", "view", str(pr), "--repo", repo, "--json", fields], timeout
    )
    if int(metadata["number"]) != pr:
        raise ConvergeError("GitHub returned metadata for a different pull request")

    head_sha = str(metadata["headRefOid"])
    base_tip_sha = str(metadata["baseRefOid"])
    base_ref = str(metadata["baseRefName"])
    head_ref = str(metadata["headRefName"])
    create_output_dir(output_dir)
    write_json(output_dir / "metadata.json", metadata)

    comparison = gh_json(
        repo_dir,
        ["api", f"repos/{repo}/compare/{base_tip_sha}...{head_sha}"],
        timeout,
    )
    merge_base_sha = str(comparison["merge_base_commit"]["sha"])

    diff = run(
        ["gh", "pr", "diff", str(pr), "--repo", repo], cwd=repo_dir, timeout=timeout
    ).stdout
    (output_dir / "diff.patch").write_text(diff, encoding="utf-8")
    write_json(
        output_dir / "issue-comments.json",
        gh_paginated_array(repo_dir, f"repos/{repo}/issues/{pr}/comments?per_page=100", timeout),
    )
    write_json(
        output_dir / "review-comments.json",
        gh_paginated_array(repo_dir, f"repos/{repo}/pulls/{pr}/comments?per_page=100", timeout),
    )
    write_json(output_dir / "threads.json", collect_review_threads(repo_dir, repo, pr, timeout))

    if fetch_refs:
        upstream = f"https://github.com/{repo}.git"
        run(
            [
                "git",
                "fetch",
                "--force",
                upstream,
                f"refs/pull/{pr}/head:refs/review-converge/pr-{pr}",
                f"refs/heads/{base_ref}:refs/review-converge/base-tip-{pr}",
            ],
            cwd=repo_dir,
            timeout=timeout,
        )
        fetched_head = git_output(repo_dir, ["rev-parse", f"refs/review-converge/pr-{pr}"], timeout)
        fetched_base_tip = git_output(
            repo_dir, ["rev-parse", f"refs/review-converge/base-tip-{pr}"], timeout
        )
        if fetched_head != head_sha or fetched_base_tip != base_tip_sha:
            raise ConvergeError("Fetched PR refs do not match the captured GitHub metadata; retry")
        merge_base_sha = git_output(
            repo_dir,
            ["merge-base", f"refs/review-converge/pr-{pr}", f"refs/review-converge/base-tip-{pr}"],
            timeout,
        )
        if merge_base_sha != str(comparison["merge_base_commit"]["sha"]):
            raise ConvergeError("Local and GitHub merge-base calculations disagree; retry")
        run(
            ["git", "update-ref", f"refs/review-converge/base-{pr}", merge_base_sha],
            cwd=repo_dir,
            timeout=timeout,
        )

    snapshot_data = {
        "source": "github",
        "repo": repo,
        "target": f"PR #{pr}",
        "pr": pr,
        "head_sha": head_sha,
        "merge_base_sha": merge_base_sha,
        "base_tip_sha": base_tip_sha,
        "base_ref": base_ref,
        "head_ref": head_ref,
        "review_ref": f"refs/review-converge/pr-{pr}" if fetch_refs else None,
        "base_review_ref": f"refs/review-converge/base-{pr}" if fetch_refs else None,
        "base_tip_review_ref": f"refs/review-converge/base-tip-{pr}" if fetch_refs else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "context_availability": "GitHub metadata, checks, reviews, comments, and threads captured",
        "constraints": snapshot_constraints(),
    }
    write_json(output_dir / "snapshot.json", snapshot_data)
    return Snapshot(
        "github", repo, f"PR #{pr}", pr, head_sha, merge_base_sha, base_tip_sha,
        base_ref, head_ref, output_dir
    )


def local_diff(repo_dir: Path, merge_base: str, head_sha: str, include_dirty: bool, timeout: int) -> str:
    committed = run(
        ["git", "diff", "--binary", f"{merge_base}..{head_sha}"],
        cwd=repo_dir,
        timeout=timeout,
    ).stdout
    if not include_dirty:
        return committed
    dirty = run(["git", "diff", "--binary", head_sha], cwd=repo_dir, timeout=timeout).stdout
    return committed + ("\n# Uncommitted tracked changes\n" + dirty if dirty else "")


def worktree_fingerprint(repo_dir: Path, head_sha: str, timeout: int) -> str:
    status = run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=repo_dir, timeout=timeout
    ).stdout
    dirty = run(["git", "diff", "--binary", head_sha], cwd=repo_dir, timeout=timeout).stdout
    return hashlib.sha256((status + "\0" + dirty).encode()).hexdigest()


def collect_local_snapshot(
    repo_dir: Path,
    base_ref: str,
    head_ref: str,
    output_dir: Path,
    timeout: int,
    include_dirty: bool,
) -> Snapshot:
    head_sha = git_output(repo_dir, ["rev-parse", "--verify", f"{head_ref}^{{commit}}"], timeout)
    base_tip_sha = git_output(repo_dir, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"], timeout)
    merge_base_sha = git_output(repo_dir, ["merge-base", base_tip_sha, head_sha], timeout)
    status = run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=repo_dir, timeout=timeout
    ).stdout
    if any(line.startswith("?? ") for line in status.splitlines()):
        raise ConvergeError(
            "Local worktree contains untracked files, which --include-dirty cannot capture; "
            "add, ignore, or remove them first"
        )
    if status and not include_dirty:
        raise ConvergeError("Local worktree is dirty; commit/stash changes or use --include-dirty")
    checked_out_head_sha = git_output(repo_dir, ["rev-parse", "--verify", "HEAD^{commit}"], timeout)
    if include_dirty and head_sha != checked_out_head_sha:
        raise ConvergeError("--include-dirty requires --head to resolve to the checked-out HEAD")
    fingerprint = worktree_fingerprint(repo_dir, head_sha, timeout) if include_dirty else None
    create_output_dir(output_dir)
    diff = local_diff(repo_dir, merge_base_sha, head_sha, include_dirty, timeout)
    (output_dir / "diff.patch").write_text(diff, encoding="utf-8")
    write_json(output_dir / "metadata.json", {
        "source": "local", "base_ref": base_ref, "head_ref": head_ref,
        "dirty_worktree": bool(status), "include_dirty": include_dirty,
    })
    for name in ("issue-comments.json", "review-comments.json"):
        write_json(output_dir / name, [])
    write_json(output_dir / "threads.json", {"reviewThreads": [], "reviews": []})
    repo = repo_dir.name
    snapshot_data = {
        "source": "local",
        "repo": repo,
        "target": f"local {base_ref}...{head_ref}",
        "pr": None,
        "head_sha": head_sha,
        "merge_base_sha": merge_base_sha,
        "base_tip_sha": base_tip_sha,
        "base_ref": base_ref,
        "head_ref": head_ref,
        "worktree_fingerprint": fingerprint,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "context_availability": "Local Git source only; GitHub threads, reviews, comments, and checks unavailable",
        "constraints": snapshot_constraints(),
    }
    write_json(output_dir / "snapshot.json", snapshot_data)
    return Snapshot(
        "local", repo, snapshot_data["target"], None, head_sha, merge_base_sha, base_tip_sha,
        base_ref, head_ref, output_dir, fingerprint
    )


def snapshot_constraints() -> dict[str, bool]:
    return {
        "source_only": True,
        "no_checkout_edits": True,
        "no_build_or_test": True,
        "no_github_writes": True,
    }


def render_prompt(template_name: str, values: dict[str, str]) -> str:
    template = (PROMPTS / template_name).read_text(encoding="utf-8")
    expected = set(PLACEHOLDER.findall(template))
    supplied = {"{{" + key + "}}" for key in values}
    missing = sorted(expected - supplied)
    if missing:
        raise ConvergeError(f"Missing prompt values for {template_name}: {', '.join(missing)}")
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    unresolved = PLACEHOLDER.findall(template)
    if unresolved:
        raise ConvergeError(f"Unresolved placeholders in {template_name}: {', '.join(unresolved)}")
    return template


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
                    raise ConvergeError(f"Claude {key} was not valid structured JSON") from exc
            return value
    raise ConvergeError("Claude JSON envelope has neither structured_output nor result")


class ReviewerAdapter(Protocol):
    name: str

    def invoke(self, prompt: str, schema_path: Path) -> Any: ...


@dataclass(frozen=True)
class ClaudeAdapter:
    repo_dir: Path
    timeout: int
    model: str | None = None
    max_budget: float | None = None
    name: str = "claude"

    def invoke(self, prompt: str, schema_path: Path) -> Any:
        schema = schema_path.read_text(encoding="utf-8")
        argv = [
            "claude", "-p", "--output-format", "json", "--json-schema", schema,
            "--permission-mode", "plan", "--allowedTools", "Read",
            "Bash(git diff:*)", "Bash(git show:*)", "Bash(git log:*)",
            "Bash(git status:*)", "Bash(git merge-base:*)",
            "--disallowedTools", "Edit", "Write", "WebFetch", "WebSearch",
            "--max-turns", "30", "--no-session-persistence",
        ]
        if self.model:
            argv.extend(["--model", self.model])
        if self.max_budget is not None:
            argv.extend(["--max-budget-usd", str(self.max_budget)])
        argv.append(prompt)
        return extract_claude_result(run(argv, cwd=self.repo_dir, timeout=self.timeout).stdout)


@dataclass(frozen=True)
class CodexAdapter:
    repo_dir: Path
    timeout: int
    model: str | None = None
    name: str = "codex"

    def invoke(self, prompt: str, schema_path: Path) -> Any:
        with tempfile.NamedTemporaryFile(
            prefix="review-converge-codex-", suffix=".json", delete=False
        ) as file:
            output_path = Path(file.name)
        try:
            argv = [
                "codex", "exec", "--sandbox", "read-only", "--ephemeral",
                "--color", "never", "--output-schema", str(schema_path),
                "--output-last-message", str(output_path), "--cd", str(self.repo_dir),
            ]
            if self.model:
                argv.extend(["--model", self.model])
            argv.append(prompt)
            run(argv, cwd=self.repo_dir, timeout=self.timeout)
            return load_json(output_path)
        finally:
            output_path.unlink(missing_ok=True)


def run_pair(claude_call: Any, codex_call: Any) -> tuple[Any, Any]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        claude_future = pool.submit(claude_call)
        codex_future = pool.submit(codex_call)
        return claude_future.result(), codex_future.result()


def response_decisions(result: dict[str, Any]) -> dict[str, tuple[Any, Any]] | None:
    responses = result.get("responses")
    if not isinstance(responses, list):
        return None
    decisions: dict[str, tuple[Any, Any]] = {}
    for response in responses:
        finding_id = response.get("finding_id")
        if not isinstance(finding_id, str) or finding_id in decisions:
            return None
        decisions[finding_id] = (response.get("decision"), response.get("corrected_severity"))
    return decisions


def finding_ids(result: dict[str, Any], field: str) -> set[str]:
    findings = result.get(field)
    if not isinstance(findings, list):
        raise ConvergeError(f"Reviewer output did not contain a {field} array")
    ids: set[str] = set()
    for finding in findings:
        finding_id = finding.get("id") if isinstance(finding, dict) else None
        if not isinstance(finding_id, str) or not finding_id:
            raise ConvergeError(f"Reviewer output contained an invalid {field} ID")
        if finding_id in ids:
            raise ConvergeError(f"Reviewer output repeated finding ID {finding_id}")
        ids.add(finding_id)
    return ids


def validate_review(result: dict[str, Any], reviewer: str) -> set[str]:
    if result.get("reviewer") != reviewer:
        raise ConvergeError(f"Expected {reviewer} review identity, got {result.get('reviewer')!r}")
    ids = finding_ids(result, "findings")
    prefix = f"{reviewer}:"
    invalid = sorted(finding_id for finding_id in ids if not finding_id.startswith(prefix))
    if invalid:
        raise ConvergeError(f"{reviewer} finding IDs must start with {prefix}: {', '.join(invalid)}")
    return ids


def validate_new_findings(result: dict[str, Any], reviewer: str) -> set[str]:
    if result.get("reviewer") != reviewer:
        raise ConvergeError(f"Expected {reviewer} reconciliation identity, got {result.get('reviewer')!r}")
    ids = finding_ids(result, "new_findings")
    prefix = f"{reviewer}:"
    invalid = sorted(finding_id for finding_id in ids if not finding_id.startswith(prefix))
    if invalid:
        raise ConvergeError(f"{reviewer} finding IDs must start with {prefix}: {', '.join(invalid)}")
    return ids


def converged(
    claude: dict[str, Any], codex: dict[str, Any], expected_finding_ids: set[str]
) -> bool:
    claude_decisions = response_decisions(claude)
    codex_decisions = response_decisions(codex)
    return bool(
        claude.get("converged") and codex.get("converged")
        and claude.get("revised_verdict") == codex.get("revised_verdict")
        and claude_decisions is not None and codex_decisions is not None
        and set(claude_decisions) == expected_finding_ids
        and set(codex_decisions) == expected_finding_ids
        and claude_decisions == codex_decisions
        and not claude.get("material_disagreements")
        and not codex.get("material_disagreements")
        and not claude.get("new_findings") and not codex.get("new_findings")
    )


def artifact_path(output_dir: Path, round_number: int, reviewer: str) -> Path:
    return output_dir / f"round-{round_number}-{reviewer}.json"


def snapshot_unchanged(snapshot: Snapshot, repo_dir: Path, timeout: int) -> bool:
    if snapshot.source == "github":
        if snapshot.pr is None:
            raise ConvergeError("GitHub snapshot is missing its pull-request number")
        data = gh_json(
            repo_dir,
            ["pr", "view", str(snapshot.pr), "--repo", snapshot.repo, "--json", "headRefOid"],
            timeout,
        )
        return str(data["headRefOid"]) == snapshot.head_sha
    if git_output(repo_dir, ["rev-parse", "--verify", f"{snapshot.head_ref}^{{commit}}"], timeout) != snapshot.head_sha:
        return False
    if snapshot.worktree_fingerprint is not None:
        return worktree_fingerprint(repo_dir, snapshot.head_sha, timeout) == snapshot.worktree_fingerprint
    return not run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=repo_dir, timeout=timeout
    ).stdout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-converge",
        description="Converge independent Claude and Codex source-only reviews.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pr", type=int, help="GitHub pull request number")
    mode.add_argument("--local", action="store_true", help="review local Git refs without GitHub access")
    parser.add_argument("--repo", help="owner/repo; inferred from the current checkout")
    parser.add_argument("--repo-dir", type=Path, default=Path.cwd(), help="local Git checkout")
    parser.add_argument("--base", help="local mode base ref")
    parser.add_argument("--head", default="HEAD", help="local mode head ref (default: HEAD)")
    parser.add_argument("--include-dirty", action="store_true", help="include tracked staged/unstaged changes in local mode")
    parser.add_argument("--rounds", type=int, default=3, help="maximum reconciliation rounds")
    parser.add_argument("--final-decider", choices=["claude", "codex"], default="codex")
    parser.add_argument("--output-dir", type=Path, help="artifact directory; must not already exist")
    parser.add_argument("--timeout", type=int, default=1800, help="seconds per external command")
    parser.add_argument("--claude-model", help="optional Claude model override")
    parser.add_argument("--codex-model", help="optional Codex model override")
    parser.add_argument("--claude-max-budget-usd", type=float, help="optional budget per Claude invocation")
    parser.add_argument("--no-fetch", action="store_true", help="GitHub mode: do not refresh local review refs")
    parser.add_argument("--dry-run", action="store_true", help="collect snapshot but do not invoke agents")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.rounds < 0 or args.rounds > 10:
        raise ConvergeError("--rounds must be between 0 and 10")
    if args.local and not args.base:
        raise ConvergeError("--local requires --base")
    if not args.local and (args.base or args.head != "HEAD" or args.include_dirty):
        raise ConvergeError("--base, --head, and --include-dirty require --local")
    if args.local and (args.repo or args.no_fetch):
        raise ConvergeError("--repo and --no-fetch are GitHub-mode options")


def execute(args: argparse.Namespace) -> Path:
    validate_args(args)
    repo_dir = args.repo_dir.resolve()
    if git_output(repo_dir, ["rev-parse", "--is-inside-work-tree"], args.timeout) != "true":
        raise ConvergeError(f"Not a Git worktree: {repo_dir}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = "local" if args.local else f"pr-{args.pr}"
    output_dir = (
        args.output_dir or Path(tempfile.gettempdir()) / "review-converge" / f"{label}-{stamp}"
    ).resolve()
    if output_dir.is_relative_to(repo_dir):
        raise ConvergeError("Output directory must be outside the reviewed checkout")
    if args.local:
        snapshot = collect_local_snapshot(
            repo_dir, args.base, args.head, output_dir, args.timeout, args.include_dirty
        )
    else:
        repo = args.repo or infer_repo(repo_dir, args.timeout)
        snapshot = collect_github_snapshot(
            repo_dir, repo, args.pr, output_dir, args.timeout, fetch_refs=not args.no_fetch
        )
    print(f"Snapshot: {output_dir}")
    print(f"Head:     {snapshot.head_sha}")
    if args.dry_run:
        print("Dry run complete; no model was invoked.")
        return output_dir

    snapshot_data = load_json(output_dir / "snapshot.json")
    common = {
        "repo": snapshot.repo,
        "target": snapshot.target,
        "source": snapshot.source,
        "head_sha": snapshot.head_sha,
        "base_sha": snapshot.merge_base_sha,
        "snapshot_dir": str(output_dir),
        "repo_dir": str(repo_dir),
        "context_availability": snapshot_data["context_availability"],
    }
    review_schema = SCHEMAS / "review.json"
    reconcile_schema = SCHEMAS / "reconciliation.json"
    final_schema = SCHEMAS / "final.json"
    adapters: dict[str, ReviewerAdapter] = {
        "claude": ClaudeAdapter(repo_dir, args.timeout, args.claude_model, args.claude_max_budget_usd),
        "codex": CodexAdapter(repo_dir, args.timeout, args.codex_model),
    }

    print("Running independent reviews...")
    initial_prompt = render_prompt("review.md", common)
    claude_review, codex_review = run_pair(
        lambda: adapters["claude"].invoke(initial_prompt + "\nYour reviewer identity is claude.", review_schema),
        lambda: adapters["codex"].invoke(initial_prompt + "\nYour reviewer identity is codex.", review_schema),
    )
    write_json(artifact_path(output_dir, 0, "claude"), claude_review)
    write_json(artifact_path(output_dir, 0, "codex"), codex_review)
    expected_finding_ids = validate_review(claude_review, "claude")
    codex_finding_ids = validate_review(codex_review, "codex")
    collision = expected_finding_ids & codex_finding_ids
    if collision:
        raise ConvergeError(f"Initial reviews reused finding IDs: {', '.join(sorted(collision))}")
    expected_finding_ids |= codex_finding_ids
    claude_latest: dict[str, Any] = claude_review
    codex_latest: dict[str, Any] = codex_review
    rounds_run = 0
    for round_number in range(1, args.rounds + 1):
        if not snapshot_unchanged(snapshot, repo_dir, args.timeout):
            raise ConvergeError("Review source changed during review; artifacts retained, rerun")
        print(f"Running reconciliation round {round_number}/{args.rounds}...")
        values = {
            **common,
            "round": str(round_number),
            "claude_latest": str(artifact_path(output_dir, round_number - 1, "claude")),
            "codex_latest": str(artifact_path(output_dir, round_number - 1, "codex")),
            "artifact_glob": str(output_dir / "round-*.json"),
            "expected_finding_ids": json.dumps(sorted(expected_finding_ids)),
        }
        prompt = render_prompt("reconcile.md", values)
        claude_latest, codex_latest = run_pair(
            lambda: adapters["claude"].invoke(prompt + "\nYour reviewer identity is claude.", reconcile_schema),
            lambda: adapters["codex"].invoke(prompt + "\nYour reviewer identity is codex.", reconcile_schema),
        )
        write_json(artifact_path(output_dir, round_number, "claude"), claude_latest)
        write_json(artifact_path(output_dir, round_number, "codex"), codex_latest)
        claude_new_ids = validate_new_findings(claude_latest, "claude")
        codex_new_ids = validate_new_findings(codex_latest, "codex")
        repeated = expected_finding_ids & (claude_new_ids | codex_new_ids)
        if repeated:
            raise ConvergeError(
                f"Reconciliation reused existing finding IDs: {', '.join(sorted(repeated))}"
            )
        rounds_run = round_number
        if converged(claude_latest, codex_latest, expected_finding_ids):
            print(f"Converged after round {round_number}.")
            break
        expected_finding_ids |= claude_new_ids | codex_new_ids

    if not snapshot_unchanged(snapshot, repo_dir, args.timeout):
        raise ConvergeError("Review source changed before final decision; rerun")
    final_values = {
        **common,
        "final_decider": args.final_decider,
        "rounds_run": str(rounds_run),
        "claude_latest": str(artifact_path(output_dir, rounds_run, "claude")),
        "codex_latest": str(artifact_path(output_dir, rounds_run, "codex")),
        "artifact_glob": str(output_dir / "round-*.json"),
    }
    print(f"Running {args.final_decider} final decision...")
    final_result = adapters[args.final_decider].invoke(
        render_prompt("final.md", final_values), final_schema
    )
    write_json(output_dir / "final.json", final_result)
    markdown = final_result.get("final_markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise ConvergeError("Final output did not contain final_markdown")
    (output_dir / "final.md").write_text(markdown.rstrip() + "\n", encoding="utf-8")
    print(f"Final review: {output_dir / 'final.md'}")
    return output_dir


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        execute(args)
    except ConvergeError as exc:
        print(f"review-converge: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("review-converge: interrupted", file=sys.stderr)
        return 130
    return 0
