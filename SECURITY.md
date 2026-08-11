# Security

`review-converge` gives AI coding agents read access to a local checkout and to captured pull-request content. Treat pull-request source, descriptions, and comments as untrusted input.

The built-in workflow:

- runs Codex in a read-only sandbox;
- runs Claude in plan mode with writes and web tools disabled;
- runs Copilot with an explicit model, builtin MCP and remote operations disabled, and only built-in view, glob, grep, and ripgrep tools; shell is unavailable;
- prohibits builds, tests, checkout changes, and GitHub writes in every prompt;
- stores artifacts outside the checkout by default;
- stops if the pull-request head changes during review.

GitHub PR mode performs read-only API queries and optional `git fetch` operations. It does not post comments, submit reviews, resolve threads, approve pull requests, or push branches. Fetching does update local refs under `refs/review-converge/*`; use `--no-fetch` to disable that behavior.

Local mode (`--local`) makes no GitHub API calls and performs no network fetch. It still writes the selected snapshot and review artifacts to the configured output directory.

Configuration is loaded only from an explicit `--config` path. The tool never auto-loads configuration from the reviewed checkout. Context files must resolve inside that checkout and are copied and hashed before model invocation.

`--instruction` and `--instruction-file` are trusted operator inputs. They may narrow review scope or add criteria, but prompts explicitly prevent them from overriding source-only and no-write constraints. Do not point `--instruction-file` at untrusted pull-request content; use `--context-file` for untrusted repository documentation.

Copilot does not currently expose the same operating-system sandbox boundary as Codex. Its safety boundary is an availability filter that exposes only read, glob, and search, plus explicit denials for shell and edit tools. Because shell is unavailable, Copilot reviews the captured patch and readable checkout files but cannot use `git show` to inspect a pinned object that differs from the worktree. Verify the installed Copilot CLI version and organization policy recorded in `run.json`; use Codex when an OS-enforced read-only sandbox or pinned-object inspection is required.

`--resume` verifies immutable inputs and completed artifact hashes before continuing. It fails closed if source, context, prompts, schemas, configuration, provider CLI versions, or recorded artifacts have changed.

These controls reduce risk but do not make arbitrary repository content trusted. Run the tool only in repositories you are willing to expose to both configured model providers. Review CLI configuration, hooks, plugins, MCP servers, and organization policies before use. Never place API keys in the repository or command arguments.

Report vulnerabilities privately to the repository maintainers rather than opening a public issue with exploit details.
