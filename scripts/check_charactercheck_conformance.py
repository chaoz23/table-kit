#!/usr/bin/env python3
"""Cold-installed table-kit -> charactercheck result conformance gate."""

import argparse
import copy
from importlib import resources
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from jsonschema import Draft202012Validator, FormatChecker

import charactercheck
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


def _derive(command, validator, root, ref, expected, status):
    value = _run([
        command, "derive", str(ref), "--table-evaluation",
    ], expected, root)
    validator.validate(value)
    contracts.require_evaluation_semantics(value)
    assert value["status"] == status
    assert value["exit_code"] == expected
    assert value["authority_status"] == "self_attested"
    assert value["subject"]["kind"] == "character"
    assert value["subject"]["entity_refs"] == []
    return value


def _write(root, name, value):
    path = root / name
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    # macOS exposes /var through /private/var; CharacterCheck deliberately
    # rejects aliasing paths, so exercise the same canonical path CI sees.
    return path.resolve()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--tablekit", default="tablekit")
    parser.add_argument("--charactercheck", default="charactercheck")
    args = parser.parse_args(argv)

    _assert_cold_install(tablekit)
    _assert_cold_install(charactercheck)
    schema = _run([args.tablekit, "contract", "evaluation"], 0, None)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    sample = json.loads(resources.files("charactercheck").joinpath(
        "sample-character.json").read_text(encoding="utf-8"))
    character_name = sample["data"]["name"]

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        sample_path = _write(root, "sample.json", sample)
        unsupported = _derive(
            args.charactercheck, validator, root, sample_path,
            2, "unsupported")
        assert unsupported["coverage"]["complete"] is False
        assert unsupported["context"]["policy_digest"].startswith("sha256:")
        assert character_name not in json.dumps(unsupported, sort_keys=True)
        repeated = _derive(
            args.charactercheck, validator, root, sample_path,
            2, "unsupported")
        assert repeated == unsupported

        reconciled = copy.deepcopy(sample)
        reconciled["data"]["inventory"] = []
        advisory = _derive(
            args.charactercheck, validator, root,
            _write(root, "advisory.json", reconciled),
            1, "checked_with_advisories")
        assert advisory["coverage"]["complete"] is True
        assert advisory["advisories"]
        assert all(item["severity"] == "advisory"
                   for item in advisory["advisories"])
        assert advisory["context"]["session_descriptor_digest"] is None

        unknown = copy.deepcopy(reconciled)
        unknown["data"]["futureMechanicalThing"] = {"value": 1}
        incomplete = _derive(
            args.charactercheck, validator, root,
            _write(root, "unknown.json", unknown),
            2, "incomplete")
        assert incomplete["errors"]

        invalid_source = copy.deepcopy(reconciled)
        invalid_source["data"]["modifiers"]["race"].append({
            "type": "bonus", "subType": "armor-class", "isGranted": True,
        })
        invalid = _derive(
            args.charactercheck, validator, root,
            _write(root, "invalid.json", invalid_source),
            2, "invalid")
        assert invalid["errors"]

        bad_ref = _derive(
            args.charactercheck, validator, root, "notanumber",
            2, "invalid")
        assert bad_ref["errors"][0]["code"] == "charactercheck.bad_ref"
        assert "notanumber" not in json.dumps(bad_ref, sort_keys=True)

    print(json.dumps({
        "status": "passed", "authority_status": "self_attested",
        "tablekit_version": tablekit.__version__,
        "charactercheck_version": charactercheck.__version__,
        "cases": ["unsupported", "checked_with_advisories", "incomplete",
                  "invalid_field", "invalid_reference", "deterministic"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
