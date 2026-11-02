"""Tests for pricing, waste detection and the honesty invariants."""

import json
import unittest

from burnmap.analyze import analyze_session
from burnmap.parser import Turn, parse_lines
from burnmap.pricing import FALLBACK_RATE, PriceBook


def turn(idx, model="claude-sonnet-4-5", inp=1000, out=100, cr=0, cw=0, tools=None):
    tools = tools or []
    return Turn(
        index=idx,
        model=model,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_write_tokens=cw,
        tools=[t[0] for t in tools],
        targets=[t[1] for t in tools],
    )


class TestPricing(unittest.TestCase):
    def test_known_model_uses_its_rate(self):
        book = PriceBook()
        # 1M input at $3 = $3.00
        self.assertAlmostEqual(book.cost("claude-sonnet-4-5", input_tokens=1_000_000), 3.0, places=6)

    def test_longest_key_wins(self):
        book = PriceBook()
        mini = book.rate_for("gpt-4o-mini")
        full = book.rate_for("gpt-4o")
        self.assertLess(mini.input, full.input)

    def test_cache_rates_are_cheaper(self):
        book = PriceBook()
        rate = book.rate_for("claude-sonnet-4-5")
        self.assertLess(rate.cache_read, rate.input)

    def test_overrides_and_default(self):
        book = PriceBook({"my-model": {"input": 1.0, "output": 2.0}, "default": {"input": 0, "output": 0}})
        self.assertAlmostEqual(book.cost("my-model-2026", input_tokens=1_000_000), 1.0, places=6)
        # Unknown models follow the "default" override instead of the fallback.
        self.assertAlmostEqual(book.cost("totally-unknown", input_tokens=1_000_000), 0.0, places=6)

    def test_unknown_model_is_flagged(self):
        book = PriceBook()
        book.rate_for("mystery-llm")
        self.assertIn("mystery-llm", book.unknown_models)
        self.assertEqual(book.rate_for("mystery-llm"), FALLBACK_RATE)

    def test_pricing_from_file(self, tmp_path=None):
        import tempfile, os

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rates.json")
            with open(path, "w") as handle:
                json.dump({"cheap": {"input": 0.1, "output": 0.2}}, handle)
            book = PriceBook.from_file(path)
        self.assertAlmostEqual(book.cost("cheap-model", input_tokens=1_000_000), 0.1, places=6)


class TestLoopDetection(unittest.TestCase):
    def test_three_identical_calls_is_a_loop(self):
        turns = [
            turn(1, tools=[("Read", "a.py")]),
            turn(2, tools=[("Read", "a.py")]),
            turn(3, tools=[("Read", "a.py")]),
        ]
        smap = analyze_session(turns)
        loops = [i for i in smap.waste if i.kind == "loops"]
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0].turns, [1, 2, 3])
        # Only the repeats are charged, not the first legitimate call.
        self.assertEqual(loops[0].tokens_wasted, 2000)

    def test_two_calls_is_not_a_loop(self):
        turns = [turn(1, tools=[("Read", "a.py")]), turn(2, tools=[("Read", "a.py")])]
        self.assertEqual([i for i in analyze_session(turns).waste if i.kind == "loops"], [])

    def test_same_tool_different_target_is_not_a_loop(self):
        turns = [turn(i, tools=[("Read", f"file{i}.py")]) for i in range(1, 5)]
        self.assertEqual([i for i in analyze_session(turns).waste if i.kind == "loops"], [])

    def test_threshold_is_configurable(self):
        turns = [turn(i, tools=[("Bash", "pytest")]) for i in range(1, 3)]
        self.assertEqual(analyze_session(turns).waste, [])
        self.assertTrue(
            [i for i in analyze_session(turns, loop_threshold=2).waste if i.kind == "loops"]
        )


class TestCacheAndCreep(unittest.TestCase):
    def test_no_cache_usage_means_no_cache_misses(self):
        turns = [turn(i, inp=50_000) for i in range(1, 4)]
        self.assertEqual([i for i in analyze_session(turns).waste if i.kind == "cache_misses"], [])

    def test_large_uncached_turn_flagged(self):
        turns = [
            turn(1, inp=100, cr=9000),
            turn(2, inp=12_000, cr=0),
        ]
        misses = [i for i in analyze_session(turns).waste if i.kind == "cache_misses"]
        self.assertEqual(len(misses), 1)
        self.assertEqual(misses[0].turns, [2])

    def test_small_uncached_turn_ignored(self):
        turns = [turn(1, inp=100, cr=9000), turn(2, inp=500, cr=0)]
        self.assertEqual([i for i in analyze_session(turns).waste if i.kind == "cache_misses"], [])

    def test_context_creep_detected_on_growing_session(self):
        turns = [turn(i, inp=1000) for i in range(1, 5)]
        turns += [turn(i, inp=9000) for i in range(5, 11)]
        creep = [i for i in analyze_session(turns).waste if i.kind == "context_creep"]
        self.assertEqual(len(creep), 1)
        self.assertGreater(creep[0].cost_usd, 0)

    def test_flat_session_has_no_creep(self):
        turns = [turn(i, inp=8000) for i in range(1, 13)]
        self.assertEqual([i for i in analyze_session(turns).waste if i.kind == "context_creep"], [])

    def test_short_session_skips_creep(self):
        self.assertEqual(
            [i for i in analyze_session([turn(1, inp=1), turn(2, inp=99_999)]).waste
             if i.kind == "context_creep"],
            [],
        )


class TestSpendMapInvariants(unittest.TestCase):
    def session(self):
        turns = [
            turn(1, inp=4000, tools=[("Read", "a.py")], cw=4000),
            turn(2, inp=200, cr=4000, tools=[("Edit", "a.py")]),
            turn(3, inp=300, cr=4200, tools=[("Bash", "pytest -q")]),
            turn(4, inp=300, cr=4200, tools=[("Bash", "pytest -q")]),
            turn(5, inp=300, cr=4200, tools=[("Bash", "pytest -q")]),
            turn(6, inp=15_000, tools=[("Read", "b.py")]),
            turn(7, inp=16_000, tools=[("Read", "c.py")]),
        ]
        return turns

    def test_waste_never_exceeds_total(self):
        smap = analyze_session(self.session())
        self.assertLessEqual(smap.waste_cost_usd, smap.total_cost_usd + 1e-9)
        self.assertLessEqual(smap.waste_share, 1.0)

    def test_totals_match_turn_sum(self):
        turns = self.session()
        smap = analyze_session(turns)
        self.assertEqual(smap.turns, len(turns))
        self.assertEqual(smap.input_tokens, sum(t.input_tokens for t in turns))
        self.assertEqual(smap.output_tokens, sum(t.output_tokens for t in turns))
        self.assertAlmostEqual(smap.total_cost_usd, sum(t.cost_usd for t in turns), places=9)

    def test_tool_attribution_sums_to_total(self):
        smap = analyze_session(self.session())
        tool_total = sum(t.cost_usd for t in smap.tools)
        self.assertAlmostEqual(tool_total, smap.total_cost_usd, places=2)

    def test_tools_ranked_by_cost(self):
        smap = analyze_session(self.session())
        costs = [t.cost_usd for t in smap.tools]
        self.assertEqual(costs, sorted(costs, reverse=True))

    def test_cache_hit_rate(self):
        smap = analyze_session([turn(1, inp=100, cr=900)])
        self.assertAlmostEqual(smap.cache_hit_rate, 0.9, places=6)

    def test_empty_session_is_safe(self):
        smap = analyze_session([])
        self.assertEqual(smap.turns, 0)
        self.assertEqual(smap.total_cost_usd, 0.0)
        self.assertEqual(smap.cost_per_turn, 0.0)
        self.assertEqual(smap.waste_share, 0.0)

    def test_parsed_sample_file_end_to_end(self):
        turns = parse_lines([
            json.dumps({
                "message": {
                    "role": "assistant",
                    "model": "claude-haiku",
                    "usage": {"input_tokens": 500, "output_tokens": 100},
                    "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}],
                }
            })
        ])
        smap = analyze_session(turns)
        self.assertEqual(smap.turns, 1)
        self.assertGreater(smap.total_cost_usd, 0)


if __name__ == "__main__":
    unittest.main()
