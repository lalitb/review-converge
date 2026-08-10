# Contributing

Thank you for improving `review-converge`.

## Development setup

The project supports Python 3.10 and newer and has no runtime dependencies.

```sh
python3 -m pip install -e .
python3 -m unittest discover -s tests -v
python3 -m review_converge --help
```

## Pull requests

- Keep reviewer execution read-only and preserve the no-GitHub-write guarantee.
- Add tests for behavior changes, especially snapshot pinning, pagination, subprocess arguments, convergence decisions, and artifact selection.
- Keep provider-specific command construction inside reviewer adapters.
- Do not silently omit unavailable context or unsupported input.
- Update the README and example output when the user-visible contract changes.

Tests should not require GitHub access or invoke an AI provider. Use mocks or temporary local Git repositories.

## Security reports

Do not open a public issue for a vulnerability with exploit details. Follow [SECURITY.md](SECURITY.md) and contact the maintainers privately.
