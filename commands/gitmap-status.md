---
description: Show gitmap reporter diagnostics — config, repo resolution, spool state, server reachability
---

Run this exact command with the Bash tool and show the user its full output verbatim:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/status.py"
```

Then, only if the output shows a problem (NOT CONFIGURED, NEGATIVE-CACHED, UNREACHABLE, no map, or a STALE CLAIM), add one or two sentences explaining what it means and the likely fix:

- **NOT CONFIGURED** — the plugin needs `server`/`token` set: re-enable the plugin to be prompted, or set `GITMAP_SERVER`/`GITMAP_TOKEN`.
- **NEGATIVE-CACHED / auth** — the token was rejected (bad scope or plan without events). Mint a `gm_` token with `events:write` on a Pro plan. Clears itself within 15 minutes of a fix.
- **no map for this repo** — the gitmap server has no map built from this repo's origin URL; build one, or set `GITMAP_MAP` to an existing map name.
- **STALE CLAIM** — a flush process died mid-flush; it self-heals within an hour, no action needed.
