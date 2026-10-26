# CLAUDE.md

Guidance for Claude Code in this repo (G'MIC Studio, a Krita docker plugin). See `HANDOFF.md` for the current state.

## The user's PC is low spec: test efficiently

- Run the smallest check that proves the change: one test file or `-k` selection, one filter on a tiny image (about 16x16). Do not run the whole suite after every edit; run it once at the end.
- Never run `tools/catalogue_check.py` over the whole catalogue (1216 filters). Use `--limit`/`--every` or check named filters one by one, with `--jobs 1`.
- One gmic process at a time, always under `nice -n 19`. No parallel jobs, no repeated long loops.
- Do not leave background jobs running. Stop what you started (by its own PID, never `pkill` by name) before moving on.
- Run tests with no `DISPLAY`. Keep scratch files in the job's temp folder.
