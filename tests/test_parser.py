"""Unit tests for the session log parsers."""

import json
import unittest

from burnmap.parser import parse_lines

ANTHROPIC = {
    "type": "assistant",
    "timestamp": "2026-11-01T10:00:00Z",
    "message": {
        "role": "assistant",
        "model": "claude-sonnet-4-5-20250929[1m]",
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 340,
            "cache_read_input_tokens": 8000,
            "cache_creation_input_tokens": 400,
        },
        "content": [
            {"type": "text", "text": "reading the file"},
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "src/a.py"}},
        ],
    },
}

OPENAI = {
    "created_at": "2026-11-01T10:01:00Z",
    "model": "gpt-4o-mini",
    "usage": {
        "prompt_tokens": 500,
        "completion_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 300},
    },
    "choices": [
        {
            "message": {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "README.md"}),
                        }
                    }
                ],
            }
        }
    ],
}

FLAT = {
    "role": "assistant",
    "model": "my-local-model",
    "input_tokens": 90,
    "output_tokens": 30,
    "tools": ["Bash"],
}


def lines(*records):
    return [json.dumps(r) for r in records]


class TestParser(unittest.TestCase):
    def test_anthropic_shape(self):
        turns = parse_lines(lines(ANTHROPIC))
        self.assertEqual(len(turns), 1)
        turn = turns[0]
        self.assertEqual(turn.input_tokens, 1200)
        self.assertEqual(turn.output_tokens, 340)
        self.assertEqual(turn.cache_read_tokens, 8000)
        self.assertEqual(turn.cache_write_tokens, 400)
        self.assertEqual(turn.tools, ["Read"])
        self.assertEqual(turn.targets, ["src/a.py"])
        # The "[1m]" context tag must not leak into the model name.
        self.assertEqual(turn.model, "claude-sonnet-4-5-20250929")

    def test_openai_shape(self):
        turns = parse_lines(lines(OPENAI))
        self.assertEqual(len(turns), 1)
        turn = turns[0]
        self.assertEqual(turn.input_tokens, 500)
        self.assertEqual(turn.output_tokens, 120)
        self.assertEqual(turn.cache_read_tokens, 300)
        self.assertEqual(turn.tools, ["read_file"])
        self.assertEqual(turn.targets, ["README.md"])

    def test_flat_shape(self):
        turns = parse_lines(lines(FLAT))
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].input_tokens, 90)
        self.assertEqual(turns[0].tools, ["Bash"])

    def test_mixed_formats_keep_order(self):
        turns = parse_lines(lines(ANTHROPIC, OPENAI, FLAT))
        self.assertEqual([t.index for t in turns], [1, 2, 3])
        self.assertEqual(turns[1].model, "gpt-4o-mini")

    def test_ignores_junk_and_useless_records(self):
        raw = [
            "",
            "not json at all",
            "# a comment line",
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
            json.dumps({"role": "assistant", "model": "x", "content": "no usage here"}),
            json.dumps([1, 2, 3]),
            json.dumps(ANTHROPIC),
        ]
        turns = parse_lines(raw)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].input_tokens, 1200)

    def test_malformed_tool_arguments_do_not_crash(self):
        record = {
            "model": "gpt-4o",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "choices": [{"message": {"tool_calls": [{"function": {"name": "run", "arguments": "{oops"}}]}}],
        }
        turns = parse_lines(lines(record))
        self.assertEqual(turns[0].tools, ["run"])

    def test_totals_and_signature(self):
        turns = parse_lines(lines(ANTHROPIC))
        turn = turns[0]
        self.assertEqual(turn.total_tokens, 1540)
        self.assertEqual(turn.tool_signature, "Read:src/a.py")


if __name__ == "__main__":
    unittest.main()
