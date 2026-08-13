---
description: Test whether a difference is real — significance across segments, survival with censoring, cohort comparison, or a spike-resistant trend
argument-hint: [what you want to test, in plain words]
allowed-tools: Bash, Read
---

Run a statistical test over the current session. Follow the `fv-stats` skill
for which test fits and how to report it; this command only picks the analysis
from what the user asked.

User input: `$ARGUMENTS`

1. **Choose the analysis.** "Is X really different / significant" → `segments`.
   "When do they leave / drop out" → `survival`. "Are newer batches worse" →
   `cohorts`. "Is it rising or falling" → `trend`.

2. **Run it:**

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stats.py" --analysis <kind> [options]
   ```

   The session must already be cleaned. If it is not, run
   `run_stage.py --stage clean` first. If a required column is missing, the CLI
   names the `--approve-features` call that builds it.

3. **Report the verdict with its uncertainty.** A non-significant difference is
   a complete answer — say it plainly rather than hedging it into a finding.
