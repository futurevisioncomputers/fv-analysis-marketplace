---
description: Cross one metric by two dimensions — which course underperforms at which branch, with an optional filter layer
argument-hint: [the combination you want, in plain words]
allowed-tools: Bash, Read
---

Cross a metric by two dimensions over the current session. Follow the
`fv-cross` skill for how to read the result; this command only picks the pair
and the filters from what the user asked.

User input: `$ARGUMENTS`

1. **See what the data supports** before choosing a pair:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_cross.py" --suggest
   ```

2. **Run the crossing:**

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_cross.py" \
       --value <column> --rows <dim> --cols <dim> [--kind rate|mean|sum|count|ratio] \
       [--filter "field = value"]
   ```

   The session must already be cleaned; if not, run
   `run_stage.py --stage clean` first.

3. **Report the guards, not just the grid.** A cell counts as a finding only
   when it beats both of its margins after correction. Exit code 3 means the
   grid computed cleanly and nothing did — that is a complete answer
   ("one dimension explains everything here"), not a failure, and it must not
   be turned into a finding by quoting the largest cell.
