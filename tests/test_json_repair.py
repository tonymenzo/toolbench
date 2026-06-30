"""
Tests for the bounded tool-call JSON repair (fix for gpt-oss-120b
emitting malformed `writefile` `data` arguments that crashed trials).

Covers the three contract points:
  - valid JSON passes through byte-identical (no happy-path change);
  - recoverable escaping faults (raw newline, lone backslash, U+202F,
    other control chars) now parse after repair;
  - a genuine Python *expression* (`"0"*3000`) is NOT made valid and is
    NOT executed — it must fall through cleanly.
"""

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from toolbench.core.json_repair import (  # noqa: E402
    parse_tool_call_arguments, repair_tool_call_json,
)


class TestValidJsonUntouched(unittest.TestCase):
    def test_identity_on_valid_json(self):
        for s in (
            '{}',
            '{"path": "out.txt", "data": "hello"}',
            '{"data": "line1\\nline2 with \\"quotes\\" and a \\\\ backslash"}',
            '{"n": 3000, "nested": {"a": [1, 2, 3]}}',
            '{"latex": "$H_T^\\\\gamma$"}',  # already correctly escaped
        ):
            # repair returns the SAME object (identity), not a re-serialized one
            self.assertIs(repair_tool_call_json(s), s)
            # and parsing still yields the right thing
            self.assertEqual(parse_tool_call_arguments(s), json.loads(s))


class TestRecoverableFaults(unittest.TestCase):
    def test_literal_newlines_in_string(self):
        raw = '{"path": "h.py", "data": "import os\nprint(os)\n"}'
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw)
        out = parse_tool_call_arguments(raw)
        self.assertEqual(out["data"], "import os\nprint(os)\n")

    def test_lone_backslash_latex(self):
        # A raw LaTeX blob: $H_T^\gamma$ — the backslash before 'g' is not
        # a valid JSON escape, so the stock parser dies here.
        raw = '{"data": "histogram of $H_T^\\gamma$ events"}'
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw)
        out = parse_tool_call_arguments(raw)
        self.assertEqual(out["data"], "histogram of $H_T^\\gamma$ events")

    def test_narrow_no_break_space_u202f(self):
        # U+202F leaked in from LaTeX/copy-paste. It is technically a raw
        # char inside the string; ensure it survives a round-trip.
        raw = '{"label": "p_T > 30 GeV"}'
        out = parse_tool_call_arguments(raw)
        self.assertIn(" ", out["label"])

    def test_other_control_char(self):
        raw = '{"data": "a\x07b\tc"}'  # bell + literal tab
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw)
        out = parse_tool_call_arguments(raw)
        self.assertEqual(out["data"], "a\x07b\tc")

    def test_realistic_writefile_blob(self):
        # The audit's worst case in one argument: literal newlines + a
        # lone-backslash LaTeX token + U+202F, all inside `data`.
        raw = (
            '{"path": "plot.py", "data": "import matplotlib\n'
            'plt.xlabel(\\"$H_T^\\gamma$\\")\n'
            '# cut: p_T > 30\n"}'
        )
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw)
        out = parse_tool_call_arguments(raw)
        self.assertEqual(out["path"], "plot.py")
        self.assertIn("$H_T^\\gamma$", out["data"])
        self.assertIn("import matplotlib\n", out["data"])


class TestPythonExpressionRejected(unittest.TestCase):
    def test_string_multiplication_not_repaired(self):
        # A Python expression, NOT JSON. We must not execute it and must
        # not fabricate a valid value — repair fails cleanly.
        raw = '{"data": "0"*3000}'
        self.assertIsNone(repair_tool_call_json(raw))
        with self.assertRaises(json.JSONDecodeError):
            parse_tool_call_arguments(raw)

    def test_string_concatenation_not_repaired(self):
        raw = '{"data": "aaa" + "bbb"}'
        self.assertIsNone(repair_tool_call_json(raw))
        with self.assertRaises(json.JSONDecodeError):
            parse_tool_call_arguments(raw)

    def test_truncated_object_not_repaired(self):
        # Structural damage is out of scope — must not be silently accepted.
        raw = '{"data": "hello", "path":'
        self.assertIsNone(repair_tool_call_json(raw))

    def test_non_string_input(self):
        self.assertIsNone(repair_tool_call_json(None))


if __name__ == "__main__":
    unittest.main()
