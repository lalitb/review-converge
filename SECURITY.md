# Security

`review-converge` gives AI coding agents read access to a local checkout and to captured pull-request content. Treat pull-request source, descriptions, and comments as untrusted input.

The built-in workflow:

- runs Codex in a read-only sandbox;
- runs Claude in plan mode with writes and web tools disabled;
- prohibits builds, tests, checkout changes, and GitHub writes in every prompt;
- stores artifacts outside the checkout by default;
- stops if the pull-request head changes during review.

GitHub PR mode performs read-only API queries and optional `git fetch` operations. It does not post comments, submit reviews, resolve threads, approve pull requests, or push branches. Fetching does update local refs under `refs/review-converge/*`; use `--no-fetch` to disable that behavior.

Local mode (`--local`) makes no GitHub API calls and performs no network fetch. It still writes the selected snapshot and review artifacts to the configured output directory.

These controls reduce risk but do not make arbitrary repository content trusted. Run the tool only in repositories you are willing to expose to both configured model providers. Review CLI configuration, hooks, plugins, MCP servers, and organization policies before use. Never place API keys in the repository or command arguments.

Report vulnerabilities privately to the repository maintainers rather than opening a public issue with exploit details.
