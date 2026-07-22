# gitmap-pro Claude Code plugin

Reports Claude Code agent activity to your [gitmap](https://gitmap.pro) server:

- **Presence** — a live "who is working where" signal (session + subagents, branch, worktree) rendered on your codebase map.
- **Attention** — a durable log of which files your agents read, search, and edit, with line ranges, aggregated locally and posted as `attention.read` / `attention.edit` / `attention.search` events.

Self-contained: Python 3 stdlib only, no pip installs. Hooks never block your session — network happens in a detached fire-and-forget child process, and a local spool absorbs server downtime.

## Install

```bash
claude marketplace add gitmap-pro/gitmap-pro-claude-plugin
claude plugin install gitmap-pro@gitmap-pro
```

You'll be prompted for your gitmap server URL and a `gm_` API token with the `events:write` scope (Pro plan). Mint one:

```bash
curl -X POST https://<your-server>/api/account/tokens \
  -H "Cookie: <session>" -d '{"scopes": ["events:write"]}'
```

### Who shows up as "you"

Sessions are labeled with your git identity (`git config user.name` / `user.email`) plus the machine hostname, so a team's map can group agent activity by contributor. Set the optional **Contributor name** plugin option (or `GITMAP_NAME`) to override the display name.

## Privacy

Events carry repo-relative file paths, line numbers, search pattern text, your git name/email, and your machine's hostname — **never file contents**. Search patterns are the only free-text capture.

## How it works

Hooks spool one line per file touch to `~/.cache/gitmap/attention/<session>.jsonl` (a single `O_APPEND` write — read/search hooks never fork or touch the network). On `Stop` (throttled to every 5 min), `SubagentStop`, and `SessionEnd`, a detached child aggregates the spool into per-file rollups — count, merged line ranges, per-tool and per-subagent counts — and posts them as `attention.*` events in at most 5 requests. Presence heartbeats ride the first batch. Server down? The spool just keeps absorbing; the next flush drains it.

Env overrides (mainly for testing): `GITMAP_SERVER`, `GITMAP_TOKEN`, `GITMAP_NAME` (contributor display name), `GITMAP_MAP` (skip repo→map resolution), `GITMAP_HOOK_DEBUG=1` (log to `~/.cache/gitmap/agent-hook.log`).

## Status

P1 (reporter core). A `/gitmap-status` debug command and an attention-focus skill land in P2. See `docs/probes.md` for the recorded hook-payload contract.
