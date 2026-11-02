"""Tests for the text/JSON renderers and the CLI contract."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from burnmap.analyze import analyze_session
from burnmap.parser import Turn
from burnmap.report import render_json, render_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "sample", "session.jsonl")


def turn(idx, inp=1000, out=100, tools=None, cr=0):
    tools = tools or []
    return Turn(
        index=idx,
        model="claude-sonnet-4-5",
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        tools=[t[0] for t in tools],
        targets=[t[1] for t in tools],
    )


class TestTextReport(unittest.TestCase):
    def test_contains_core_sections(self):
        text = render_text(analyze_session([turn(1, tools=[("Read", "a.py")])]))
        for section in ("session spend map", "total cost", "COST BY MODEL", "COST BY TOOL", "WASTE"):
            self.assertIn(section, text)

    def test_clean_session_says_so(self):
        text = render_text(analyze_session([turn(1)]))
        self.assertIn("Clean session", text)

    def test_waste_section_lists_fix(self):
        turns = [turn(i, tools=[("Bash", "pytest")]) for i in range(1, 5)]
        text = render_text(analyze_session(turns))
        self.assertIn("[loops]", text)
        self.assertIn("fix:", text)

    def test_unknown_model_note(self):
        turns = [Turn(index=1, model="brand-new-llm", input_tokens=10, output_tokens=5)]
        text = render_text(analyze_session(turns))
        self.assertIn("unknown models priced at the fallback rate", text)

    def test_no_ansi_codes(self):
        text = render_text(analyze_session([turn(1)]))
        self.assertNotIn("\x1b", text)


class TestJsonReport(unittest.TestCase):
    def test_json_round_trip(self):
        smap = analyze_session([turn(1, tools=[("Read", "a.py")])])
        payload = json.loads(render_json(smap))
        self.assertEqual(payload["turns"], 1)
        self.assertEqual(payload["tokens"]["input"], 1000)
        self.assertIn("models", payload)
        self.assertIn("tools", payload)
        self.assertIn("waste", payload)

    def test_waste_items_have_required_keys(self):
        turns = [turn(i, tools=[("Bash", "pytest")]) for i in range(1, 4)]
        payload = json.loads(render_json(analyze_session(turns)))
        item = payload["waste"]["items"][0]
        for key in ("kind", "headline", "turns", "tokens_wasted", "cost_usd", "fix"):
            self.assertIn(key, item)


class TestCli(unittest.TestCase):
    def run_cli(self, *args, stdin=None):
        cmd = [sys.executable, os.path.join(ROOT, "burnmap"), *args]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(ROOT, "src")
        return subprocess.run(cmd, capture_output=True, text=True, input=stdin, env=env, timeout=60)

    def test_sample_file_runs(self):
        result = self.run_cli(SAMPLE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("session spend map", result.stdout)

    def test_json_flag(self):
        result = self.run_cli(SAMPLE, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["turns"], 15)

    def test_stdin_dash(self):
        with open(SAMPLE) as handle:
            data = handle.read()
        result = self.run_cli("-", stdin=data)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fail_over_gate_trips(self):
        result = self.run_cli(SAMPLE, "--fail-over", "0.0001")
        self.assertEqual(result.returncode, 2)
        self.assertIn("exceeds", result.stderr)

    def test_fail_over_gate_passes(self):
        result = self.run_cli(SAMPLE, "--fail-over", "999")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_empty_input_fails_loudly(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
            handle.write('{"type":"user"}\n')
            path = handle.name
        try:
            result = self.run_cli(path)
        finally:
            os.unlink(path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no usable usage records", result.stderr)

    def test_missing_file_fails_loudly(self):
        result = self.run_cli("/nope/does-not-exist.jsonl")
        self.assertEqual(result.returncode, 1)

    def test_pricing_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rates.json")
            with open(path, "w") as handle:
                json.dump({"claude": {"input": 0, "output": 0}}, handle)
            result = self.run_cli(SAMPLE, "--json", "--pricing", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        # Every claude-* model is now free; the single gpt-4o-mini turn keeps its
        # own rate, which proves overrides are matched per model, not globally.
        claude = [m for name, m in payload["models"].items() if name.startswith("claude")]
        self.assertTrue(claude)
        self.assertTrue(all(m["cost_usd"] == 0.0 for m in claude))
        self.assertLess(payload["total_cost_usd"], 0.001)

    def test_bad_loop_threshold_rejected(self):
        result = self.run_cli(SAMPLE, "--loop-threshold", "1")
        self.assertNotEqual(result.returncode, 0)

    def test_top_flag(self):
        result = self.run_cli(SAMPLE, "--top", "2")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
