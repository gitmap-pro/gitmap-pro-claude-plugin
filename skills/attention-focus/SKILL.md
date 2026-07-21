---
name: attention-focus
description: Record what you are currently working on and why as a note on the project's gitmap map. Use at natural moments — starting a distinct piece of work, switching files or subsystems, or finishing something notable. Complements the automatic attention tracking (which logs *where* you worked) with the *why*.
---

Post a short focus note to the project's gitmap map:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/focus.py" --path <repo-relative-path> --note "<one sentence: what you're doing and why>"
```

Rules:

- `--path` is the repo-relative file or directory the work centers on; omit it only for repo-wide work.
- The note is one sentence, present tense, specific: "rewriting the flush claim so concurrent hooks can't double-post", not "working on code".
- Post at most one note per distinct piece of work — this is a journal entry, not a heartbeat. The automatic tracker already records every file touch.
- The script prints whether the note was recorded; if it reports a configuration problem, mention it to the user once and move on — never retry in a loop.
- Run it from inside the repository (any subdirectory works).
