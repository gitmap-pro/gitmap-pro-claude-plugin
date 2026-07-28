"""Pure outcome logic (codemap#267): goal lines, command classification,
test-output parsing, commit-show parsing, the status transition ratchet."""
import outcomes


class TestIntentLine:
    def test_first_line_collapsed_and_capped(self):
        assert outcomes.intent_line(
            "  fix   the flaky\tflush race\nand then some detail\n") \
            == "fix the flaky flush race"
        assert len(outcomes.intent_line("x" * 500)) == outcomes.INTENT_MAX

    def test_trivial_and_junk_prompts_say_nothing(self):
        assert outcomes.intent_line("continue") == ""
        assert outcomes.intent_line("yes") == ""
        assert outcomes.intent_line("\n\n  \n") == ""
        assert outcomes.intent_line(None) == ""
        assert outcomes.intent_line(12) == ""

    def test_leading_blank_lines_skipped(self):
        assert outcomes.intent_line("\n\nrefactor the spool claim path") \
            == "refactor the spool claim path"


class TestCommandClassification:
    def test_commit_commands(self):
        rx = outcomes.COMMIT_CMD_RX
        assert rx.search("git commit -m 'x'")
        assert rx.search("cd wt && git add -A && git commit -q -m x")
        assert rx.search("git merge --no-edit origin/main")
        assert rx.search("git cherry-pick abc123")
        assert not rx.search("git pull --rebase")   # other people's commits
        assert not rx.search("git log | grep commit")
        assert not rx.search("ls -la")

    def test_test_commands(self):
        rx = outcomes.TEST_CMD_RX
        assert rx.search("python -m pytest tests/ -q")
        assert rx.search("pytest -x tests/test_db.py")
        assert rx.search("npm test")
        assert rx.search("yarn run test --watch=false")
        assert rx.search("go test ./...")
        assert rx.search("cargo test")
        assert not rx.search("git commit -m 'add tests'")
        assert not rx.search("echo testing")

    def test_pr_create(self):
        assert outcomes.PR_CREATE_RX.search('gh pr create --title "x"')
        assert not outcomes.PR_CREATE_RX.search("gh pr view 12")

    def test_command_of_and_response_text(self):
        assert outcomes.command_of({"command": "ls"}) == "ls"
        assert outcomes.command_of(None) == ""
        assert outcomes.response_text(
            {"stdout": "a", "stderr": "b"}) == "a\nb"
        assert outcomes.response_text("plain") == "plain"
        assert outcomes.response_text(None) == ""


class TestParseTestOutput:
    def test_pytest_pass_fail_error(self):
        assert outcomes.parse_test_output(
            "== 12 passed, 2 warnings in 3.1s ==") \
            == {"value": "pass", "passed": 12, "failed": 0}
        assert outcomes.parse_test_output("== 2 failed, 10 passed ==") \
            == {"value": "fail", "passed": 10, "failed": 2}
        assert outcomes.parse_test_output("1 failed, 3 passed, 2 errors") \
            == {"value": "fail", "passed": 3, "failed": 3}

    def test_jest_cargo_go(self):
        assert outcomes.parse_test_output(
            "Tests:  1 failed, 12 passed, 13 total") \
            == {"value": "fail", "passed": 12, "failed": 1}
        assert outcomes.parse_test_output(
            "test result: ok. 31 passed; 0 failed; 2 ignored") \
            == {"value": "pass", "passed": 31, "failed": 0}
        out = outcomes.parse_test_output("ok  \texample.com/pkg\t0.01s")
        assert out["value"] == "pass" and out["passed"] is None
        assert outcomes.parse_test_output(
            "--- FAIL: TestX (0.00s)\nFAIL")["value"] == "fail"

    def test_unrecognizable_output_posts_nothing(self):
        assert outcomes.parse_test_output("") is None
        assert outcomes.parse_test_output("collected 0 items") is None
        assert outcomes.parse_test_output(None) is None


class TestParseCommitShow:
    def test_full_shortstat(self):
        text = "ada@example.com\x1fadd the thing\n\n" \
               " 2 files changed, 10 insertions(+), 3 deletions(-)\n"
        assert outcomes.parse_commit_show(text) \
            == ("ada@example.com", "add the thing", 2, 10, 3)

    def test_insertions_only_and_empty(self):
        text = "a@b.c\x1fdocs\n 1 file changed, 5 insertions(+)\n"
        assert outcomes.parse_commit_show(text) == ("a@b.c", "docs", 1, 5, 0)
        assert outcomes.parse_commit_show("") == ("", "", None, None, None)


class TestNextStatus:
    def test_happy_path_ratchet(self):
        assert outcomes.next_status(None, "prompt") == "started"
        assert outcomes.next_status("started", "waiting") == "blocked"
        assert outcomes.next_status("blocked", "prompt") == "started"
        assert outcomes.next_status("started", "pr") == "review"
        assert outcomes.next_status("review", "end") == "done"

    def test_dedupe_and_guards(self):
        assert outcomes.next_status("started", "prompt") is None
        assert outcomes.next_status("blocked", "waiting") is None
        assert outcomes.next_status("done", "prompt") is None
        # nothing started: no task to block or finish
        assert outcomes.next_status(None, "waiting") is None
        assert outcomes.next_status(None, "end") is None
        # a PR waiting on its human reviewer is not "blocked"
        assert outcomes.next_status("review", "waiting") is None
        assert outcomes.next_status(None, "nonsense") is None
