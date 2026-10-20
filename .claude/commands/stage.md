---
description: Run one implementation stage in this session (usage: /stage N)
---

You are running stage $ARGUMENTS of the staged implementation. This session covers this stage only.

1. Read `CLAUDE.md` (already loaded), then `docs/kb/progress.md` and `docs/kb/stages.md`. Confirm with `git branch --merged main` that the previous stage branch is merged and with `git status` that the working tree is clean; if not, stop and tell me.
2. Read the files in the `read:` column of stage $ARGUMENTS. Do not read other knowledge-base files unless the stage needs them.
3. Create `stage/$ARGUMENTS-<short-name>` from `main` (name as in the status table of `progress.md`).
4. Post the design note (at most one screen). If it contains a real choice, stop and wait for my decision. Topics already listed under "Decided" in `docs/kb/design-topics.md` are settled; do not reopen them.
5. Implement with tests, inside this stage's scope only. Do not stage or commit anything: I review the full diff of the stage before any commit.
6. At the end run `make check`, update `docs/kb/progress.md` (status table, latest summary, carry-over, proposed commit message: one commit per stage), post the summary and STOP. Leave everything uncommitted.
7. After my review, apply the requested fixes to the working tree, run `make check` again and update the summary; still no commit. Commit only when I say so, as one commit for the stage. Do not merge, rebase, reset or push; merging is my gate action.
