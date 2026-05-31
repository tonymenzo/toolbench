"""Sandbox-cleanup keep-list behavior (eval/core/runner.py).

Pins two properties that are easy to regress:
  * agent-authored code executed via code-running tools (RunPythonTool)
    is materialized to artifacts/scripts/ even though those tools run a
    temp file *outside* the sandbox;
  * third-party tool machinery (MadGraph's `bin/internal/`) is pruned
    from the `.py` FULL-copy rule, while genuine deliverables (the UFO
    module) survive for regrade.
"""

import tempfile
import unittest
from pathlib import Path

from toolbench.core.runner import TrialRunner
from toolbench.core.trajectory import ToolCall


def _tc(name, args, ok=True):
    return ToolCall(t=0.0, name=name, args=args, duration_s=0.5,
                    ok=ok, result_summary="ok")


class TestArtifactCleanup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.sandbox = root / "sandbox"
        self.trial = root / "trial"
        self.trial.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, data=b"x"):
        p = self.sandbox / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def test_agent_scripts_materialized_in_order(self):
        tcs = [
            _tc("RunPython", {"code": "import numpy as np\nnp.save('m', [1])"}),
            _tc("RunCommand", {"command": "ls -R ."}),          # not a script
            _tc("run_python", {"code": "print('plot')"}, ok=False),
        ]
        self._write("output/answer.json", b"{}")
        TrialRunner._cleanup_sandbox(self.sandbox, self.trial, tool_calls=tcs)

        scripts = sorted(p.name for p in (self.trial / "artifacts" / "scripts").glob("*"))
        # RunCommand's one-liner is excluded; the two code blocks are kept, ordered.
        self.assertEqual(scripts, ["001_runpython.py", "002_run_python.py"])
        body = (self.trial / "artifacts" / "scripts" / "001_runpython.py").read_text()
        self.assertIn("np.save", body)
        self.assertIn("tool-call #1", body)   # original trajectory index preserved

    def test_no_scripts_dir_when_no_code_calls(self):
        self._write("output/answer.json", b"{}")
        TrialRunner._cleanup_sandbox(self.sandbox, self.trial,
                                     tool_calls=[_tc("RunCommand", {"command": "ls"})])
        self.assertFalse((self.trial / "artifacts" / "scripts").exists())

    def test_machinery_pruned_deliverable_kept(self):
        self._write("data/mg/mg_output/bin/internal/misc.py", b"# machinery\n")
        self._write("ufo/S1/parameters.py", b"# deliverable\n")
        self._write("output/plot.png", b"\x89PNG")
        TrialRunner._cleanup_sandbox(self.sandbox, self.trial, tool_calls=[])

        art = self.trial / "artifacts"
        kept_py = {str(p.relative_to(art)) for p in art.rglob("*.py")}
        self.assertIn("ufo/S1/parameters.py", kept_py)
        self.assertTrue(all("bin/internal" not in p for p in kept_py))
        self.assertTrue((art / "output" / "plot.png").exists())

    def test_sandbox_removed(self):
        self._write("output/answer.json", b"{}")
        TrialRunner._cleanup_sandbox(self.sandbox, self.trial, tool_calls=[])
        self.assertFalse(self.sandbox.exists())


if __name__ == "__main__":
    unittest.main()
