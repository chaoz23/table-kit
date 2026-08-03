#!/usr/bin/env python3
"""Cold-installed table-kit -> dmcheck portfolio conformance gate."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from jsonschema import Draft202012Validator, FormatChecker

import dmcheck
import tablekit
from tablekit import contracts


def _run(command, expected=0, cwd=None):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode != expected:
        raise AssertionError(
            "%s exited %d, expected %d\nstdout:\n%s\nstderr:\n%s" % (
                " ".join(command), result.returncode, expected,
                result.stdout, result.stderr))
    return result


def _json(command, expected=0, cwd=None):
    result = _run(command, expected=expected, cwd=cwd)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError("command did not emit one JSON value: %s" % command) from exc


def _assert_cold_install(module):
    module_path = Path(module.__file__).resolve()
    prefix = Path(sys.prefix).resolve()
    if prefix not in module_path.parents:
        raise AssertionError("%s was imported outside the test environment: %s" % (
            module.__name__, module_path))


def _validators(tablekit_command):
    evaluation_schema = _json([tablekit_command, "contract", "evaluation"])
    event_schema = _json([tablekit_command, "contract", "event"])
    return (
        Draft202012Validator(evaluation_schema, format_checker=FormatChecker()),
        Draft202012Validator(event_schema, format_checker=FormatChecker()),
    )


def _assert_evaluation(value, validator, status, exit_code, complete,
                       error_code=None):
    validator.validate(value)
    contracts.require_evaluation_semantics(value)
    assert value["status"] == status
    assert value["exit_code"] == exit_code
    assert value["authority_status"] == "self_attested"
    assert value["coverage"]["complete"] is complete
    if error_code is not None:
        assert error_code in {item["code"] for item in value["errors"]}


def _write_jsonl(path, values):
    path.write_text("".join(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        for value in values), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--tablekit", default="tablekit")
    parser.add_argument("--dmcheck", default="dmcheck")
    args = parser.parse_args(argv)

    _assert_cold_install(tablekit)
    _assert_cold_install(dmcheck)
    evaluation_validator, event_validator = _validators(args.tablekit)

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        # The canonical fixture intentionally contains a transport gap. The
        # consumer must preserve the gap as incomplete, never as a false clean.
        golden = _json([args.tablekit, "contract", "golden"])
        for event in golden:
            event_validator.validate(event)
        contracts.require_event_stream(golden)
        golden_path = root / "golden.jsonl"
        _write_jsonl(golden_path, golden)
        golden_result = _json(
            [args.dmcheck, "run-events", str(golden_path), "--gm", "gm-dan",
             "--table-evaluation"], expected=2, cwd=root)
        _assert_evaluation(
            golden_result, evaluation_validator, "incomplete",
            2, False, "table_event.transport_gap")
        assert golden_result["findings"] == []
        assert golden_result["cursor"]["checked_through_event_id"] == "evt-010"

        # Exercise complete and findings envelopes across the same installed
        # boundary, not only refusal behavior.
        clean = json.loads(json.dumps(golden[3:6]))
        for sequence, event in enumerate(clean, 1):
            event["session_sequence"] = sequence
        clean[0]["causation_id"] = None
        clean_path = root / "clean.jsonl"
        _write_jsonl(clean_path, clean)
        clean_result = _json([
            args.dmcheck, "run-events", str(clean_path), "--gm", "gm-dan",
            "--table-evaluation"], cwd=root)
        _assert_evaluation(
            clean_result, evaluation_validator, "checked_clean", 0, True)
        assert clean_result["findings"] == []
        assert clean_result["errors"] == []

        finding_events = json.loads(json.dumps([golden[5], golden[7]]))
        finding_events[0]["session_sequence"] = 1
        finding_events[0]["causation_id"] = None
        finding_events[0]["payload"]["resolves_obligation_ids"] = []
        finding_events[1]["session_sequence"] = 2
        finding_events[1]["causation_id"] = None
        finding_path = root / "finding.jsonl"
        _write_jsonl(finding_path, finding_events)
        finding_result = _json([
            args.dmcheck, "run-events", str(finding_path), "--gm", "gm-dan",
            "--table-evaluation"], expected=1, cwd=root)
        _assert_evaluation(
            finding_result, evaluation_validator, "findings", 1, True)
        assert "evt-008" in finding_result["findings"][0]["evidence_refs"]

        # A legacy narration preserves its native source ID but receives a
        # deterministic migration ID. Unknown legacy roles cannot fabricate
        # eligible conduct evidence, so the consumer must remain incomplete.
        legacy_path = root / "legacy.jsonl"
        _write_jsonl(legacy_path, [{
            "ts": 1.0, "type": "event", "actor": "GM", "text": "Begin.",
            "source": "discord", "source_id": "message-1",
        }])
        first_events = root / "legacy-events-1.jsonl"
        second_events = root / "legacy-events-2.jsonl"
        first_summary = _json([
            args.tablekit, "migrate-events", "--ledger", str(legacy_path),
            "--campaign", "ci", "--session", "legacy",
            "--out", str(first_events)], cwd=root)
        second_summary = _json([
            args.tablekit, "migrate-events", "--ledger", str(legacy_path),
            "--campaign", "ci", "--session", "legacy",
            "--out", str(second_events)], cwd=root)
        assert first_events.read_bytes() == second_events.read_bytes()
        assert first_summary["output_sha256"] == second_summary["output_sha256"]
        assert first_summary["output_sha256"] == "sha256:" + hashlib.sha256(
            first_events.read_bytes()).hexdigest()
        migrated = [json.loads(line) for line in first_events.read_text(
            encoding="utf-8").splitlines()]
        for event in migrated:
            event_validator.validate(event)
        contracts.require_event_stream(migrated)
        assert migrated[0]["source"]["native_id"] == "message-1"
        assert migrated[0]["event_id"] != "message-1"
        legacy_result = _json([
            args.dmcheck, "run-events", str(first_events), "--gm", "GM",
            "--table-evaluation"], expected=2, cwd=root)
        _assert_evaluation(
            legacy_result, evaluation_validator, "incomplete",
            2, False, "evidence.no_eligible_rules")
        assert legacy_result["findings"] == []

        # Redaction is explicit migration evidence. The consumer must refuse
        # to infer the absence of a conduct obligation from hidden prose.
        redacted_path = root / "redacted.jsonl"
        _write_jsonl(redacted_path, [{
            "ts": 2.0, "type": "qa.inbound", "seat": "Rowan", "chars": 12,
            "source": "discord", "source_id": "message-redacted",
        }])
        redacted_events = root / "redacted-events.jsonl"
        _json([
            args.tablekit, "migrate-events", "--ledger", str(redacted_path),
            "--campaign", "ci", "--session", "redacted",
            "--out", str(redacted_events)], cwd=root)
        redacted_result = _json([
            args.dmcheck, "run-events", str(redacted_events), "--gm", "GM",
            "--table-evaluation"], expected=2, cwd=root)
        _assert_evaluation(
            redacted_result, evaluation_validator, "incomplete",
            2, False, "table_event.content_redacted")
        assert redacted_result["findings"] == []

    print(json.dumps({
        "status": "passed", "authority_status": "self_attested",
        "tablekit_version": tablekit.__version__,
        "dmcheck_version": dmcheck.__version__,
        "cases": ["checked_clean", "findings", "golden_gap",
                  "legacy_determinism", "redacted_content"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
