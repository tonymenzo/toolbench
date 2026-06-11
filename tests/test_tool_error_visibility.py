"""Tool-call failures must be visible in trajectories and logs.

Orchestral tools report failure as strings ("Error: <kind>\\n- Reason:
..."), not dicts — before this, such calls were classified ok=True and
rendered as clean successes, so a trial with 23 consecutive
path-resolution failures read as healthy in console.log. Pins:
string-error classification, reason extraction, the rendered ✗ line in
the log, and the n_tool_errors counts in the trial records.
"""

import tempfile
import unittest
from pathlib import Path

from toolbench.core.trajectory import (
    Trajectory, TrajectoryHook, _classify_ok, _extract_error_msg,
)

try:
    import orchestral  # noqa: F401  (ToolHook base import in trajectory)
    HAVE = True
except Exception:
    HAVE = False

_ORCH_ERROR = ("Error: File Not Found\n"
               "- Reason: Run card does not exist at data/run_T5Wg/card.cmnd\n"
               "- Check tool implementation")


class TestClassification(unittest.TestCase):
    def test_orchestral_error_string_is_failure(self):
        self.assertFalse(_classify_ok(_ORCH_ERROR))
        self.assertFalse(_classify_ok("Error: Execution Error\n- Reason: x"))

    def test_ordinary_string_result_is_success(self):
        self.assertTrue(_classify_ok('{"status": "ok", "n_events": 3}'))
        # 'Error' mentioned mid-result is not a failure marker.
        self.assertTrue(_classify_ok("wrote log: no Error lines found"))

    def test_dict_contract_unchanged(self):
        self.assertFalse(_classify_ok({"ok": False}))
        self.assertFalse(_classify_ok({"error": "boom"}))
        self.assertTrue(_classify_ok({"ok": True}))

    def test_reason_extracted(self):
        msg = _extract_error_msg(_ORCH_ERROR)
        self.assertIn("File Not Found", msg)
        self.assertIn("Run card does not exist", msg)   # the actionable part


@unittest.skipUnless(HAVE, "orchestral not importable")
class TestLogRendering(unittest.TestCase):
    def test_failed_call_renders_error_line_in_console_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "console.log"
            traj = Trajectory()
            hook = TrajectoryHook(traj, verbose=False, log_path=log)
            hook.before_call("heptapod__PythiaFromRunCard",
                             {"run_card": "data/run_T5Wg/card.cmnd"})
            hook.after_call("heptapod__PythiaFromRunCard", _ORCH_ERROR)
            hook.close()
            self.assertFalse(traj.tool_calls[0].ok)
            text = log.read_text()
            self.assertIn("Run card does not exist", text)   # reason visible
            self.assertEqual(traj.to_metadata_dict()["n_tool_errors"], 1)

    def test_successful_call_stays_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "console.log"
            traj = Trajectory()
            hook = TrajectoryHook(traj, verbose=False, log_path=log)
            hook.before_call("t", {})
            hook.after_call("t", '{"status": "ok"}')
            hook.close()
            self.assertTrue(traj.tool_calls[0].ok)
            self.assertEqual(traj.to_metadata_dict()["n_tool_errors"], 0)


if __name__ == "__main__":
    unittest.main()
