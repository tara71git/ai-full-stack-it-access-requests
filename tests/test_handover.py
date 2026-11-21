import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from intake.extraction import ROOT
from test_operations import cli
from test_routing import OWNER, SECURITY
from test_state import route_tuple


class HandoverTests(unittest.TestCase):
    def test_t23_readme_smoke_flow_and_verification_handover(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        smoke = next((block for block in re.findall(r"```bash\n(.*?)\n```", readme, re.S)
                      if "mktemp -d" in block and "python3 -m intake" in block), None)
        self.assertIsNotNone(smoke, "README needs an executable clean-state walkthrough")
        self.assertIn("env -u API_KEY python3 -m unittest discover -s tests -v", readme)
        self.assertIn("API_KEY", readme)
        self.assertIn("system owner for a decision", readme)
        with tempfile.TemporaryDirectory() as directory:
            environ = {key: value for key, value in os.environ.items() if key != "API_KEY"}
            environ["TMPDIR"] = directory
            run = subprocess.run(["bash", "-eu", "-c", smoke], cwd=ROOT, env=environ,
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            work, = Path(directory).iterdir()
            self.assertEqual(len((work / "snapshot/extractions.jsonl").read_text().splitlines()), 20)
            queue = cli(work / "intake.sqlite3", "queue")
            self.assertEqual([route_tuple(row) for row in queue[:2]], [OWNER, SECURITY])
            self.assertEqual(cli(work / "intake.sqlite3", "pending"), [])
            history = cli(work / "intake.sqlite3", "history", "IT-001.json")
            self.assertGreaterEqual(len(history), 2)
            decoder = json.JSONDecoder()
            output = run.stdout.strip()
            reports = []
            while output:
                value, end = decoder.raw_decode(output)
                reports.append(value)
                output = output[end:].strip()
            accuracy = reports[-1]
            self.assertEqual((accuracy["exact_matches"], accuracy["comparisons"]), (252, 252))
        verification = (ROOT / "deliverables/verification.md").read_text(encoding="utf-8")
        for detail in ("400", "2,000", "SQLite", "interruption", "Observed", "API_KEY"):
            self.assertIn(detail, verification)
