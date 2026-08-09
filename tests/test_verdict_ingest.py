"""qc.verdict ingestion — the rules rail's provenance lands in the ledger.

unittest on purpose; see test_tablekit.py's module docstring.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tablekit import cli  # noqa: E402
from tablekit.events import Ledger, SchemaError, make  # noqa: E402

COMPLETED = {
    "event": "request.completed",
    "schema_version": "srdcheck.observability/1.0",
    "engine": {"name": "srdcheck", "version": "0.9.0"},
    "adapters": [{"name": "srd-5.2.1", "version": "0.2.1"}],
    "query_type": "concentration.check",
    "exit_code": 0,
    "outcome": "legal",
    "duration_ms": 0.046,
    "request_id": "turn-047",
    "verdict_id": "sha256:6e29f15cc3f918114f0f97e06b6f5897fcad9b514f2",
}

STARTED = {
    "event": "request.started",
    "schema_version": "srdcheck.observability/1.0",
    "engine": {"name": "srdcheck", "version": "0.9.0"},
    "query_type": "concentration.check",
    "request_id": "turn-047",
}


class VerdictIngest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="tk-verdict-")
        self.path = os.path.join(d, "session.jsonl")

    def _rows(self):
        with open(self.path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _run(self, stdin_text, *extra):
        old = sys.stdin
        sys.stdin = io.StringIO(stdin_text)
        try:
            with redirect_stdout(io.StringIO()):
                return cli.main(["verdict", *extra, "--ledger", self.path])
        finally:
            sys.stdin = old

    def test_ingests_completed_events_only(self):
        stream = "\n".join([
            json.dumps(STARTED),
            json.dumps(COMPLETED),
            '{ not json',
            '{"verdict": "legal", "exit_code": 0}',  # human verdict, no id
            "  ",
        ])
        self.assertEqual(self._run(stream), 0)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        rec = rows[0]
        self.assertEqual(rec["type"], "qc.verdict")
        self.assertEqual(rec["tool"], "srdcheck@0.9.0")
        self.assertEqual(rec["query_type"], "concentration.check")
        self.assertEqual(rec["outcome"], "legal")
        self.assertEqual(rec["exit_code"], 0)
        self.assertEqual(rec["adapters"], "srd-5.2.1@0.2.1")
        self.assertEqual(rec["request_id"], "turn-047")
        self.assertTrue(rec["verdict_id"].startswith("sha256:"))

    def test_mixed_pipe_with_pretty_verdict_json(self):
        # `srdcheck query ... --trace 2>&1` interleaves pretty-printed verdict
        # JSON (multi-line, so no line parses as an object with an event key)
        # with the observability lines. Only the completed line lands.
        pretty = json.dumps({"verdict": "legal", "exit_code": 0}, indent=2)
        stream = json.dumps(COMPLETED) + "\n" + pretty + "\n"
        self.assertEqual(self._run(stream), 0)
        self.assertEqual(len(self._rows()), 1)

    def test_empty_stream_refuses(self):
        err = io.StringIO()
        old = sys.stderr
        sys.stderr = err
        try:
            self.assertEqual(self._run("no events here\n"), 2)
        finally:
            sys.stderr = old
        self.assertIn("request.completed", err.getvalue())
        self.assertFalse(os.path.exists(self.path))

    def test_exit_two_verdicts_are_first_class(self):
        refused = dict(COMPLETED, exit_code=2, outcome="cannot-adjudicate",
                       verdict_id="sha256:beef")
        self.assertEqual(self._run(json.dumps(refused)), 0)
        rec = self._rows()[0]
        self.assertEqual(rec["exit_code"], 2)
        self.assertEqual(rec["outcome"], "cannot-adjudicate")

    def test_schema_refuses_negative_exit_code(self):
        with self.assertRaises(SchemaError):
            make("qc.verdict", tool="x@1", query_type="q", outcome="legal",
                 exit_code=-1, verdict_id="sha256:x")

    def test_dmcheck_compat_lane_is_qc(self):
        # dmcheck reads only play-lane types; qc.verdict must not be one.
        from tablekit.events import PLAY_TYPES, lane_of
        self.assertNotIn("qc.verdict", PLAY_TYPES)
        self.assertEqual(lane_of("qc.verdict"), "qc")

    def test_ledger_append_roundtrip(self):
        led = Ledger(self.path)
        led.append("qc.verdict", tool="srdcheck@0.9.0", query_type="q",
                   outcome="illegal", exit_code=1, verdict_id="sha256:aa")
        self.assertEqual(self._rows()[0]["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
