import json

import attention


def touch(k="read", p="a.py", l=None, tool="Read", agent="", t=1000.0,
          pattern=None):
    d = {"t": t, "k": k, "p": p, "l": l, "tool": tool, "agent": agent}
    if pattern:
        d["pattern"] = pattern
    return d


class TestMergeRanges:
    def test_coalesces_overlap_and_adjacent(self):
        merged, trunc = attention.merge_ranges([[5, 10], [11, 20], [8, 12],
                                                [40, 45]])
        assert merged == [[5, 20], [40, 45]]
        assert trunc is False

    def test_cap_sets_truncated(self):
        ranges = [[i * 10, i * 10 + 1] for i in range(1, 30)]
        merged, trunc = attention.merge_ranges(ranges, cap=20)
        assert len(merged) == 20
        assert trunc is True

    def test_clamps_to_one_based_and_drops_invalid(self):
        merged, _ = attention.merge_ranges([[0, 5], [-3, 2], ["x", 9],
                                            [7, 3], [4]])
        assert merged == [[1, 5]]


class TestAggregate:
    def test_single_coalesced_range_goes_into_anchor(self):
        evs = attention.aggregate([touch(l=[5, 10]), touch(l=[11, 15])],
                                  corr="s")
        assert evs[0]["anchor"] == {"path": "a.py", "lines": [5, 15]}
        assert evs[0]["payload"]["ranges"] == [[5, 15]]

    def test_disjoint_ranges_mean_path_only_anchor(self):
        evs = attention.aggregate([touch(l=[1, 5]), touch(l=[100, 110])],
                                  corr="s")
        assert evs[0]["anchor"] == {"path": "a.py"}
        assert evs[0]["payload"]["ranges"] == [[1, 5], [100, 110]]

    def test_whole_file_read_has_no_lines(self):
        evs = attention.aggregate([touch()], corr="s")
        assert evs[0]["anchor"] == {"path": "a.py"}
        assert "ranges" not in evs[0]["payload"]

    def test_groups_by_kind_and_path_value_is_count(self):
        evs = attention.aggregate(
            [touch(), touch(), touch(k="edit", tool="Edit"),
             touch(p="b.py")], corr="s")
        by = {(e["type"], e["anchor"].get("path", "")): e for e in evs}
        assert by[("attention.read", "a.py")]["value"] == 2
        assert by[("attention.edit", "a.py")]["value"] == 1
        assert by[("attention.read", "b.py")]["value"] == 1
        assert evs[0]["value"] == 2      # biggest group first

    def test_tools_agents_and_window(self):
        evs = attention.aggregate(
            [touch(t=10.0, agent="abc123"), touch(t=20.0)], corr="sess-full")
        p = evs[0]["payload"]
        assert p["tools"] == {"Read": 2}
        assert p["agents"] == {"abc123": 1}
        assert (p["t0"], p["t1"]) == (10.0, 20.0)
        assert evs[0]["corr"] == "sess-full"

    def test_search_patterns_unique_capped(self):
        touches = [touch(k="search", p="", tool="Grep", pattern="p%d" % i)
                   for i in range(15)] + \
                  [touch(k="search", p="", tool="Grep", pattern="p0")]
        evs = attention.aggregate(touches, corr="s")
        assert evs[0]["anchor"] == {}            # repo-wide search
        assert len(evs[0]["payload"]["patterns"]) == attention.PATTERNS_CAP
        assert evs[0]["payload"]["patterns"][0] == "p0"


class TestBatches:
    def test_split_and_overflow(self):
        evs = [{"i": i} for i in range(130)]
        batches, dropped = attention.split_batches(evs)
        assert len(batches) == 5
        assert all(len(b) <= 20 for b in batches)
        assert len(dropped) == 30

    def test_small_flush_is_one_batch(self):
        batches, dropped = attention.split_batches([{"i": 1}])
        assert len(batches) == 1 and dropped == []


class TestCodec:
    def test_roundtrip_and_torn_line_dropped(self):
        good = attention.encode_touch("read", "a.py", [1, 5], "Read", "ag1")
        search = attention.encode_touch("search", "src", None, "Grep", "",
                                        pattern="x" * 500)
        text = good + "\n" + '{"torn": ' + "\n" + search + "\nnot json\n"
        out = attention.decode_lines(text)
        assert [d["k"] for d in out] == ["read", "search"]
        assert out[0]["l"] == [1, 5]
        assert len(out[1]["pattern"]) == 200     # pattern capped
        assert json.loads(good)["agent"] == "ag1"
