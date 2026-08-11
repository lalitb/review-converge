# Measured public-PR examples

These case studies are pinned, source-only demonstrations run on 2026-08-10. They are not a benchmark and do not establish that convergence improves review quality in general. Provider-reported missing cost fields remain unknown.

| Pull request | Rounds | Invocations | Reported Claude cost | Final verdict | Human assessment |
| --- | ---: | ---: | ---: | --- | --- |
| [otel-arrow #3677](pr-3677.md) | 1 | 5 | $1.005666 | approve | No accepted findings; matched merged outcome |
| [otel-arrow #3594](pr-3594.md) | 0 | 3 | $0.434648 | approve | No accepted findings; matched merged outcome |

The first case exercises reconciliation. The second is a lower-cost baseline with two independent reviews and a final decision but no reconciliation. Codex did not report monetary cost, so these are reported-cost lower bounds, not total run costs.
