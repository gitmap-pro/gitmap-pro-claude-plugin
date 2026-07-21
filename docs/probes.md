# P0 probe findings (recorded 2026-07-21, Claude Code hook payloads)

Captured via `scripts/probe.py` on a live headless session (Read/Grep/Glob/Edit + one general-purpose subagent). Raw capture: 13 events. These findings are the contract P1's parser is written against.

## Probe answers

1. **PostToolUse fires for tool calls made inside subagents, and the payload carries `agent_id` + `agent_type`** (e.g. `agent_id: "a66808bf…"`, `agent_type: "general-purpose"`). Main-session tool calls carry no `agent_id` key. → Per-touch subagent attribution is possible; spool lines record `agent` when present.
2. **Edit `tool_response.structuredPatch` exists**: list of hunks `{oldStart, oldLines, newStart, newLines, lines}`. Edit touch ranges = `[oldStart, oldStart+oldLines-1]` per hunk. `Write` response shape differs (whole-file) — path-only.
3. **`SubagentStart` and `SubagentStop` both exist** and carry `agent_id`/`agent_type`. The subagent-spawn tool is currently named **`Agent`** (a `PreToolUse` with `tool_name: "Agent"` was captured; docs/older harnesses say `Task`) — irrelevant to us since SubagentStart/Stop replace tool-matcher spawn detection entirely.
4. **Manifest**: `.claude-plugin/plugin.json`; hooks at `hooks/hooks.json` (plugin root); distribution = repo as its own marketplace (`.claude-plugin/marketplace.json`); `userConfig` values reach hook processes as `CLAUDE_PLUGIN_OPTION_<KEY>` env vars (`sensitive: true` → secure storage, not settings.json).

## Field inventory (what P1 parses)

| Event | Fields used |
|---|---|
| all | `hook_event_name`, `session_id`, `cwd`; `agent_id`/`agent_type` when in a subagent |
| `PostToolUse` Read | `tool_input.file_path`, optional `tool_input.offset`/`limit` (absent = whole file) |
| `PostToolUse` Grep/Glob | `tool_input.pattern`, optional `tool_input.path` (absent = cwd) |
| `PostToolUse` Edit | `tool_input.file_path`, `tool_response.structuredPatch[].oldStart/oldLines` |
| `PostToolUse` Write/NotebookEdit | `tool_input.file_path` (path-only) |
| `SubagentStart`/`SubagentStop` | `agent_id`, `agent_type` |
| `SessionEnd` | `reason` |

Other observed-but-unused fields: `prompt_id`, `tool_use_id`, `duration_ms`, `permission_mode`, `effort`, `last_assistant_message`, `stop_hook_active`, `background_tasks`, `session_crons`, `agent_transcript_path`, SessionStart `source`.

## Consequences for the reporter (updates to the approved plan)

- Hook table: use `SubagentStart`/`SubagentStop` (drop the `PreToolUse` `Task` matcher fallback).
- Spool line: `{"t","k","p","l","tool","agent"}` with `agent` = `agent_id` or empty.
- Flushed events: `payload.agents` = per-`agent_id` touch counts (previously gated on this probe).
- Subagent actor ids: `cc-<sess8>-sub-<agent8>` using the real `agent_id` prefix (stable, no counter needed).
- Edit events get real line ranges via structuredPatch (previously "path-only unless probed").
