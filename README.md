# Cost Runaway Diagnostic Package

Run a health-check on your **AI agent's cost controls** — locally, read-only.
**No secret values and no file contents ever leave your machine.** The report
contains only names, counts, versions and recommendations, so it is safe to
share back with us for a cost-remediation proposal.

## What it checks

- **Usage tracking** — are model calls metered at all (per-call token
  accounting, usage logs, tracking libraries), or is the bill a monthly
  surprise?
- **Budget & limits** — is there a spend cap or monthly limit configured, and
  is it enforced?
- **Unbounded retry loops** — source files with `while True`-style loops
  calling model APIs with no max-retry count or backoff (the classic way a
  quiet afternoon becomes a five-figure bill)
- **Output caps** — model-calling files that set no `max_tokens` limit
- **Caching** — repeated prompts with no response cache
- **Model choice** — flagship models hardcoded with no cheaper fallback path

## Quick start (2 steps)

Requires Python 3.8+. **No dependencies to install** — standard library only.

1. Unzip and point it at the directory where your agent project lives:

   ```bash
   python cost_diagnose.py /path/to/your/agent/project
   ```

2. Open the report — written to `cost_diag_reports/cost_diagnostic_report.md`
   (and `.json` for machine reading).

## Options

```bash
python cost_diagnose.py <project_dir> \
    --output ./my-reports \      # where reports are written
    --json                       # also print JSON to stdout
python cost_diagnose.py <project_dir> \
    --compare cost_diag_reports/cost_diagnostic_report.json
                                 # diff with a previous run: new/resolved/persisting
```

## Fix verification: finding IDs and before/after comparison

Every finding carries a stable ID (e.g. `COST-NO-TRACKING`,
`COST-UNBOUNDED-RETRY:<file>`, `COST-NO-MAX-TOKENS`) and a rough self-fix
effort estimate in person-hours. After fixing, re-run with `--compare`
against the previous JSON report: the report lists **resolved**, **new**,
**severity changes** and **persisting** findings. Use "high N→0" as the
acceptance criterion instead of a gut feeling.

## Reading the report

Findings are ranked **high → medium → low → info**. Each finding carries a
concrete recommendation. Typical first-run results on an unreviewed
deployment: unbounded retry loops (high), no usage tracking (medium), no
budget cap (medium), no output caps (medium).

## Sharing the report

Send `cost_diagnostic_report.md` (or the `.json` file) back to us — we will
turn it into a phased cost-remediation proposal: cap the runaway paths first,
then add tracking and budgets, then optimize model routing.

## License

MIT — see [LICENSE](LICENSE).
