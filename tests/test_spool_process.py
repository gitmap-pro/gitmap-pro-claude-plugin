import json
import multiprocessing
import os
import time

import attention


def _writer(spool_dir, sess, n, tag):
    for i in range(n):
        attention.append_touch(
            spool_dir, sess,
            attention.encode_touch("read", "%s-%d.py" % (tag, i), None,
                                   "Read", ""))


class TestConcurrency:
    def test_two_writers_interleave_whole_lines(self, tmp_path):
        d = str(tmp_path)
        procs = [multiprocessing.Process(target=_writer,
                                         args=(d, "s1", 200, tag))
                 for tag in ("a", "b")]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        with open(attention.spool_path(d, "s1")) as f:
            lines = f.read().splitlines()
        assert len(lines) == 400
        for ln in lines:
            json.loads(ln)          # every line parses: no torn writes

    def test_claim_race_single_winner(self, tmp_path):
        d = str(tmp_path)
        attention.append_touch(d, "s1", attention.encode_touch(
            "read", "a.py", None, "Read", ""))
        spool = attention.spool_path(d, "s1")
        first = attention.claim(spool)
        second = attention.claim(spool)
        assert first and ".flushing." in first
        assert second is None

    def test_unclaim_reappends_leftover(self, tmp_path):
        d = str(tmp_path)
        attention.append_touch(d, "s1", attention.encode_touch(
            "read", "a.py", None, "Read", ""))
        spool = attention.spool_path(d, "s1")
        c = attention.claim(spool)
        attention.unclaim(c, [{"t": 1.0, "k": "read", "p": "b.py",
                               "l": None, "tool": "Read", "agent": ""}])
        assert not os.path.exists(c)
        out = attention.decode_lines(open(spool).read())
        assert [t["p"] for t in out] == ["b.py"]


class TestLifecycle:
    def test_bounds_drop_oldest(self, tmp_path):
        d = str(tmp_path)
        for i in range(attention.SPOOL_MAX_LINES + 500):
            attention.append_touch(d, "s1", attention.encode_touch(
                "read", "f%d.py" % i, None, "Read", ""))
        spool = attention.spool_path(d, "s1")
        assert attention.enforce_bounds(spool) is True
        out = attention.decode_lines(open(spool).read())
        assert len(out) == attention.SPOOL_MAX_LINES
        assert out[0]["p"] == "f500.py"          # oldest dropped

    def test_stale_claim_recovered_by_sweep(self, tmp_path):
        d = str(tmp_path)
        attention.append_touch(d, "s1", attention.encode_touch(
            "read", "a.py", None, "Read", ""))
        c = attention.claim(attention.spool_path(d, "s1"))
        old = time.time() - attention.STALE_CLAIM_AGE - 10
        os.utime(c, (old, old))
        attention.sweep(d, "other")
        assert not os.path.exists(c)
        assert os.path.exists(attention.spool_path(d, "s1"))

    def test_sweep_flags_orphans_and_deletes_ancient(self, tmp_path):
        d = str(tmp_path)
        for sess, age in (("fresh", 60),
                          ("orphan", attention.ORPHAN_FLUSH_AGE + 60),
                          ("ancient", attention.ORPHAN_DELETE_AGE + 60)):
            attention.append_touch(d, sess, attention.encode_touch(
                "read", "a.py", None, "Read", ""))
            p = attention.spool_path(d, sess)
            t = time.time() - age
            os.utime(p, (t, t))
        got = attention.sweep(d, "active")
        assert got == ["orphan"]
        assert not os.path.exists(attention.spool_path(d, "ancient"))
        assert os.path.exists(attention.spool_path(d, "fresh"))

    def test_flush_due_rules(self, tmp_path):
        d = str(tmp_path)
        assert attention.flush_due(d, "s1", "SessionEnd") is True
        assert attention.flush_due(d, "s1", "SubagentStop") is True
        assert attention.flush_due(d, "s1", "Stop") is False   # no spool
        attention.append_touch(d, "s1", attention.encode_touch(
            "read", "a.py", None, "Read", ""))
        assert attention.flush_due(d, "s1", "Stop") is True
        attention.touch_stamp(d, "s1")
        assert attention.flush_due(d, "s1", "Stop") is False   # throttled
        assert attention.flush_due(d, "s1", "PostToolUse") is False
        for i in range(attention.FLUSH_LINE_THRESHOLD):
            attention.append_touch(d, "s1", attention.encode_touch(
                "read", "file-%04d.py" % i, None, "Read", ""))
        assert attention.flush_due(d, "s1", "PostToolUse") is True
