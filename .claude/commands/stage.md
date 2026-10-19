---
description: Run one implementation stage in this session (usage: /stage N)
---

You are running stage $ARGUMENTS of the staged implementation. This session covers this stage only.

1. Read `CLAUDE.md` (already loaded), then `docs/kb/progress.md` and `docs/kb/stages.md`. Confirm with `git branch --merged main` that the previous stage branch is merged; if it is not, stop and tell me.
2. Read the files in the `read:` column of stage $ARGUMENTS. Do not read other knowledge-base files unless the stage needs them.
3. Create `stage/$ARGUMENTS-<short-name>` from `main` (name as in the status table of `progress.md`).
4. Post the design note (at most one screen). If it contains a real choice, stop and wait for my decision. Topics already listed under "Decided" in `docs/kb/design-topics.md` are settled; do not reopen them.
5. Implement with tests, inside this stage's scope only. Commit in small conventional steps.
6. At the end run `make check`, update `docs/kb/progress.md` (status table, latest summary, carry-over), commit, post the summary and STOP. Do not merge, rebase, reset or push; merging is my gate action.
