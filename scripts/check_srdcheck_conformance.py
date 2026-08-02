#!/usr/bin/env python3
"""Cold-installed table-kit -> srdcheck result conformance gate."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from jsonschema import Draft202012Validator, FormatChecker

import srdcheck
import tablekit
from tablekit import contracts


def _run(command, expected, cwd):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode != expected:
        raise AssertionError(
            "%s exited %d, expected %d\nstdout:\n%s\nstderr:\n%s" % (
                " ".join(command), result.returncode, expected,
                result.stdout, result.stderr))
    return json.loads(result.stdout)


def _assert_cold_install(module):
    module_path = Path(module.__file__).resolve()
    prefix = Path(sys.prefix).resolve()
    if prefix not in module_path.parents:
        raise AssertionError("%s was imported outside the test environment: %s" % (
            module.__name__, module_path))


def _query(command, validator, root, query_type, params, expected, status):
    value = _run([
        command, "query", query_type,
        json.dumps(params, sort_keys=True, separators=(",", ":")),
        "--table-evaluation",
    ], expected, root)
    validator.validate(value)
    contracts.require_evaluation_semantics(value)
    assert value["status"] == status
    assert value["exit_code"] == expected
    assert value["authority_status"] == "self_attested"
    assert value["subject"]["kind"] == "rules_query"
    assert value["context"]["policy_digest"].startswith("sha256:")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--tablekit", default="tablekit")
    parser.add_argument("--srdcheck", default="srdcheck")
    args = parser.parse_args(argv)

    _assert_cold_install(tablekit)
    _assert_cold_install(srdcheck)
    schema = _run([args.tablekit, "contract", "evaluation"], 0, None)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        save = {"modifier": 2, "dc": 10, "d20_result": 12,
                "save_ability": "dex", "saver_conditions": []}
        clean = _query(
            args.srdcheck, validator, root, "save.check", save,
            0, "checked_clean")
        assert clean["coverage"]["complete"] is True

        # A failed saving throw is still a completely adjudicated rules query;
        # it is not an illegal action or a portfolio finding.
        failed_save = dict(save, d20_result=2)
        resolved_failure = _query(
            args.srdcheck, validator, root, "save.check", failed_save,
            0, "checked_clean")
        assert resolved_failure["findings"] == []

        illegal = _query(
            args.srdcheck, validator, root, "mage-hand.use",
            {"kind": "attack"}, 1, "findings")
        assert "mage-hand.cant-attack" in illegal["findings"][0]["evidence_refs"]
        repeated = _query(
            args.srdcheck, validator, root, "mage-hand.use",
            {"kind": "attack"}, 1, "findings")
        assert repeated == illegal

        missing = _query(
            args.srdcheck, validator, root, "save.check", {"modifier": 2},
            2, "incomplete")
        assert missing["errors"][0]["code"] == "srdcheck.missing_fact"

        unsupported = _query(
            args.srdcheck, validator, root, "spell.facts",
            {"name": "Hexblade"}, 2, "unsupported")
        assert unsupported["errors"][0]["code"] == \
            "srdcheck.unsupported_content"

        invalid = _query(
            args.srdcheck, validator, root, "save.check",
            {"modifier": 2, "dc": 10, "bogus": 1}, 2, "invalid")
        assert invalid["errors"][0]["code"] == "srdcheck.invalid_input"

    print(json.dumps({
        "status": "passed", "authority_status": "self_attested",
        "tablekit_version": tablekit.__version__,
        "srdcheck_version": srdcheck.__version__,
        "cases": ["checked_clean", "resolved_failed_save", "findings",
                  "missing_fact", "unsupported_content", "invalid_input"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
