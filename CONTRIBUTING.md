# Contributing

Thank you for improving `review-converge`.

## Development setup

The project supports Python 3.10 and newer. Install the Copilot extra when changing its adapter or schema-validation path.

```sh
python3 -m pip install -e '.[copilot]'
python3 -m unittest discover -s tests -v
python3 -m review_converge --help
```

## Pull requests

- Keep reviewer execution read-only and preserve the no-GitHub-write guarantee.
- Add tests for behavior changes, especially snapshot pinning, pagination, subprocess arguments, convergence decisions, resume invariants, usage accounting, and artifact selection.
- Keep provider-specific command construction inside reviewer adapters.
- Keep exactly two stable reviewer slots; provider and model names must never become paths or finding namespaces.
- Do not silently omit unavailable context or unsupported input.
- Treat configuration and context from the reviewed repository as untrusted. Never add implicit configuration discovery.
- Update the README and example output when the user-visible contract changes.
- Keep session capabilities ordered and explicit: `review` < `propose` < `edit`.
- Never add implicit patch application, arbitrary shell execution, or publication
  to the session REPL. Mutable actions require a durable proposal and explicit
  operator confirmation.

Tests should not require GitHub access or invoke an AI provider. Use mocks or temporary local Git repositories.

## Security reports

Do not open a public issue for a vulnerability with exploit details. Follow [SECURITY.md](SECURITY.md) and contact the maintainers privately.
