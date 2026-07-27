# P0 probe findings (recorded 2026-07-21, Claude Code hook payloads)

Captured via `scripts/probe.py` on a live headless session (Read/Grep/Glob/Edit + one general-purpose subagent). Raw capture: 13 events. These findings are the contract P1's parser is written against.

The probe script was replaced by `report.py` in P1; to re-run it, restore it with `git show fa7ee56:scripts/probe.py > /tmp/probe.py`, point a throwaway `--settings` file's hooks at it, and drive one headless `claude -p` session in a scratch git repo.

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
| `SessionStart` | `source` (into actor meta) |
| `SessionEnd` | `reason` (into the presence clear value, `"ended: <reason>"`) |

Other observed-but-unused fields: `prompt_id`, `tool_use_id`, `duration_ms`, `permission_mode`, `effort`, `last_assistant_message`, `stop_hook_active`, `background_tasks`, `session_crons`, `agent_transcript_path`.

## Re-probe 2026-07-27 (Claude Code 2.1.220) — model capture

Same method, 9 events (SessionStart, 4 × PostToolUse, SubagentStart/Stop, Stop, SessionEnd) driven with `--model sonnet`.

1. **Still no `model` anywhere in a hook payload**, and no model-bearing env var reaches hook processes (`CLAUDE_*` gives entrypoint, session id, pid, effort, project dir — nothing about the model). The layered resolver keeps the payload key first anyway, so a future Claude Code that adds one is picked up without a code change.
2. **`transcript_path` is present on every event**, including events fired inside a subagent — it always points at the *root session* transcript. That makes it a usable model source from any hook, not just the ones that carry a `source`.
3. **Transcript lines**: `{"type": "assistant", "message": {"model": "claude-sonnet-5", …}, "isSidechain": bool, "gitBranch": …}`, one JSON object per line, appended. Newest assistant line = current model, so a bounded tail read (64KB, widened once) answers it without parsing the whole file. Subagent turns land in a *separate* file (`<session>/subagents/agent-<id>.jsonl`) in this version, but `isSidechain` has meant "not the root model" historically — the scan skips those lines either way.
4. **`agent_transcript_path`** appears only on `SubagentStop` — too late to give a subagent its own model at registration, so subagent meta reports the session model.
5. `agent_id` is longer than 8 chars (`a0abb4ddf717a5138`); actor ids keep using the first 8, unchanged.
6. `SessionStart` is still the only payload carrying `source` — a later hook can't reconstruct it, which is why the meta refresh carries forward the last value it sent rather than blanking the field.

## Consequences for the reporter (updates to the approved plan)

- Hook table: use `SubagentStart`/`SubagentStop` (drop the `PreToolUse` `Task` matcher fallback).
- Spool line: `{"t","k","p","l","tool","agent"}` with `agent` = `agent_id` or empty.
- Flushed events: `payload.agents` = per-`agent_id` touch counts (previously gated on this probe).
- Subagent actor ids: `cc-<sess8>-sub-<agent8>` using the real `agent_id` prefix (stable, no counter needed).
- Edit events get real line ranges via structuredPatch (previously "path-only unless probed").
