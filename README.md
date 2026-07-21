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

## Privacy

Events carry repo-relative file paths, line numbers, and search pattern text — **never file contents**. Search patterns are the only free-text capture.

## Status

P0 (payload probe + scaffold). The reporter lands in P1; a `/gitmap-status` debug command and an attention-focus skill land in P2. See `docs/probes.md` for recorded hook-payload findings.
