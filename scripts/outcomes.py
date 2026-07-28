"""Pure intent/outcome logic: goal lines, command classification, test-output
parsing, status transitions (codemap#267).

No HTTP and no hook parsing here — report.py owns those, same split as
attention.py. Everything is deterministic and unit-testable. Stdlib only.

The emissions this module feeds (contract: gitmap docs/EVENTS.md, "Intent
and outcomes"):
- attention.focus  — the session's one-line goal, from its first real prompt
- task.status      — started / blocked / review / done transitions
- sweep.completed  — payload.subtype "tests", parsed from test-runner output
- vcs.commit       — one event per commit observed, subject + diff stats
"""
import re

INTENT_MAX = 140
INTENT_MIN = 12         # shorter first prompts ("continue", "yes") say
                        # nothing about the goal; wait for a real one

STATUSES = ("started", "blocked", "review", "done")

# Commands that can land a commit of the session's own: worth a rev-parse
# to see whether HEAD moved. `git pull` moves HEAD too, but to other
# people's commits — not this session's outcome, so it isn't here.
COMMIT_CMD_RX = re.compile(          # (?<!-) skips flags like --rebase
    r"\bgit\b[^|;&]*\s(?<!-)(commit|merge|cherry-pick|revert|rebase|am)\b")

PR_CREATE_RX = re.compile(r"\bgh\s+pr\s+create\b")

TEST_CMD_RX = re.compile(
    r"\b(pytest|py\.test|jest|vitest|mocha|rspec|tox|nox|ctest"
    r"|go\s+test|cargo\s+(?:test|nextest)"
    r"|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b"
    r"|make\s+(?:test|check)\b"
    r"|python3?\s+-m\s+(?:pytest|unittest))")


def intent_line(prompt):
    """One-line goal from a user prompt, or "" when it says nothing.

    Not transcript mirroring: the first non-empty line only, whitespace
    collapsed, capped. Trivial acknowledgements don't count as a goal.
    """
    if not isinstance(prompt, str):
        return ""
    for raw in prompt.splitlines():
        line = " ".join(raw.split()).strip()
        if line:
            break
    else:
        return ""
    if len(line) < INTENT_MIN:
        return ""
    return line[:INTENT_MAX]


def command_of(tool_input):
    ti = tool_input if isinstance(tool_input, dict) else {}
    cmd = ti.get("command")
    return cmd if isinstance(cmd, str) else ""


def response_text(tool_response):
    """Bash hook tool_response is a {stdout, stderr} dict (older harnesses:
    a plain string); flatten to one searchable text."""
    if isinstance(tool_response, dict):
        return "\n".join(str(tool_response.get(k) or "")
                         for k in ("stdout", "stderr"))
    return tool_response if isinstance(tool_response, str) else ""


def parse_test_output(text):
    """Test-runner verdict from command output, or None when the output has
    no recognizable summary (post nothing rather than guess).

    Returns {"value": "pass"|"fail", "passed": int|None, "failed": int|None}.
    """
    if not text:
        return None
    # pytest / generic "N passed, M failed" summaries
    p = re.search(r"(\d+) passed", text)
    f = re.search(r"(\d+) failed", text)
    e = re.search(r"(\d+) error(?:s)?\b", text)
    if p or f:
        failed = (int(f.group(1)) if f else 0) + (int(e.group(1)) if e else 0)
        return {"value": "fail" if failed else "pass",
                "passed": int(p.group(1)) if p else None,
                "failed": failed}
    # jest / vitest: "Tests:  1 failed, 12 passed, 13 total"
    m = re.search(r"Tests:\s+(?:(\d+) failed, )?(?:(\d+) skipped, )?"
                  r"(\d+) passed", text)
    if m:
        failed = int(m.group(1) or 0)
        return {"value": "fail" if failed else "pass",
                "passed": int(m.group(3)), "failed": failed}
    # cargo: "test result: ok. 12 passed; 0 failed; ..."
    m = re.search(r"test result: (ok|FAILED)\. (\d+) passed; (\d+) failed",
                  text)
    if m:
        return {"value": "pass" if m.group(1) == "ok" else "fail",
                "passed": int(m.group(2)), "failed": int(m.group(3))}
    # go test: per-package ok/FAIL lines, no counts
    if re.search(r"^--- FAIL|^FAIL\b", text, re.MULTILINE):
        return {"value": "fail", "passed": None, "failed": None}
    if re.search(r"^ok\s+\S", text, re.MULTILINE):
        return {"value": "pass", "passed": None, "failed": None}
    return None


def parse_commit_show(text):
    """(email, subject, files, adds, dels) from
    `git show --shortstat --format=%ae%x1f%s HEAD`.

    The first non-empty line is "author-email\\x1fsubject"; the shortstat
    line, when the commit touched anything, looks like "2 files changed,
    10 insertions(+), 3 deletions(-)" with each tail piece optional.
    """
    email, subject, files, adds, dels = "", "", None, None, None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"(\d+) files? changed"
                     r"(?:, (\d+) insertions?\(\+\))?"
                     r"(?:, (\d+) deletions?\(-\))?", line)
        if m:
            files = int(m.group(1))
            adds = int(m.group(2) or 0)
            dels = int(m.group(3) or 0)
        elif not subject and "\x1f" in line:
            email, subject = line.split("\x1f", 1)
    return email, subject, files, adds, dels


def next_status(last, event):
    """The transition to post (or None) for a lifecycle event, given the
    last status actually posted. Dedupe lives here so every caller gets the
    same ratchet: prompt -> started (also un-blocks), waiting-on-human ->
    blocked, PR opened -> review, session end -> done (terminal).
    """
    want = {"prompt": "started", "waiting": "blocked",
            "pr": "review", "end": "done"}.get(event)
    if want is None or last == "done" or want == last:
        return None
    if last is None and want in ("blocked", "done"):
        return None       # nothing ever started: no task to block or finish
    if want == "blocked" and last == "review":
        return None       # a PR under review waiting on a human is normal
    return want
