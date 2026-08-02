import copy
import json
import os
import subprocess
import sys
import unittest

from jsonschema import Draft202012Validator, FormatChecker

from tablekit import contracts


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluation_schema = contracts.schema("evaluation")
        cls.event_schema = contracts.schema("event")
        Draft202012Validator.check_schema(cls.evaluation_schema)
        Draft202012Validator.check_schema(cls.event_schema)
        cls.evaluation_validator = Draft202012Validator(
            cls.evaluation_schema, format_checker=FormatChecker())
        cls.event_validator = Draft202012Validator(
            cls.event_schema, format_checker=FormatChecker())

    def test_every_status_and_authority_has_a_valid_packaged_fixture(self):
        path = os.path.join(os.path.dirname(contracts.__file__),
                            "evaluation-fixtures-1.0.json")
        with open(path, encoding="utf-8") as stream:
            fixtures = json.load(stream)
        for fixture in fixtures:
            with self.subTest(fixture=fixture["evaluation_id"]):
                self.evaluation_validator.validate(fixture)
                contracts.require_evaluation_semantics(fixture)
        statuses = {item["status"] for item in fixtures}
        authorities = {item["authority_status"] for item in fixtures}
        self.assertEqual(statuses, set(self.evaluation_schema["properties"]["status"]["enum"]))
        self.assertEqual(authorities, {"self_attested", "host_attested"})

    def test_clean_cannot_hide_gap_or_incomplete_coverage(self):
        path = os.path.join(os.path.dirname(contracts.__file__),
                            "evaluation-fixtures-1.0.json")
        with open(path, encoding="utf-8") as stream:
            clean = json.load(stream)[0]
        for mutation in (
            lambda item: item["cursor"].update(gap_state="detected"),
            lambda item: item["coverage"].update(complete=False),
            lambda item: item.update(exit_code=2),
        ):
            candidate = copy.deepcopy(clean)
            mutation(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(Exception):
                self.evaluation_validator.validate(candidate)

    def test_cross_field_counts_cannot_claim_complete_coverage(self):
        path = os.path.join(os.path.dirname(contracts.__file__),
                            "evaluation-fixtures-1.0.json")
        with open(path, encoding="utf-8") as stream:
            clean = json.load(stream)[0]
        for field, value in (("compatible", 11), ("eligible", 11),
                             ("evaluated", 0), ("skipped", 1)):
            candidate = copy.deepcopy(clean)
            candidate["coverage"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                contracts.require_evaluation_semantics(candidate)

    def test_host_attestation_requires_protected_binding(self):
        path = os.path.join(os.path.dirname(contracts.__file__),
                            "evaluation-fixtures-1.0.json")
        with open(path, encoding="utf-8") as stream:
            host_result = [item for item in json.load(stream)
                           if item["authority_status"] == "host_attested"][0]
        host_result["context"]["session_descriptor_digest"] = None
        with self.assertRaises(Exception):
            self.evaluation_validator.validate(host_result)

        host_event = copy.deepcopy(contracts.golden_session()[0])
        host_event["source"]["attestation"] = "host_attested"
        with self.assertRaises(Exception):
            self.event_validator.validate(host_event)

    def test_golden_session_is_valid_ordered_and_covers_required_cases(self):
        events = contracts.golden_session()
        for event in events:
            with self.subTest(event=event["event_id"]):
                self.event_validator.validate(event)
        self.assertEqual(len({event["event_id"] for event in events}), len(events))
        contracts.require_event_stream(events)
        self.assertEqual([event["session_sequence"] for event in events],
                         list(range(1, len(events) + 1)))
        kinds = {event["event_type"] for event in events}
        self.assertTrue({"session.opened", "session.closed", "transport.gap",
                         "narration.obligation", "narration.observed",
                         "evaluation.completed"}.issubset(kinds))
        reactions = [event for event in events
                     if event["event_type"] == "action.declared"]
        self.assertEqual(reactions[0]["payload"]["legal_timing"], "reaction")

    def test_unknown_event_type_is_rejected_for_adapter_to_report_unsupported(self):
        event = copy.deepcopy(contracts.golden_session()[0])
        event["event_type"] = "state.future"
        with self.assertRaises(Exception):
            self.event_validator.validate(event)

    def test_stream_refuses_duplicate_missing_out_of_order_and_mixed_session(self):
        events = contracts.golden_session()
        variants = []
        duplicate = copy.deepcopy(events)
        duplicate[1]["event_id"] = duplicate[0]["event_id"]
        variants.append(duplicate)
        missing = copy.deepcopy(events)
        del missing[3]
        variants.append(missing)
        out_of_order = copy.deepcopy(events)
        out_of_order[2], out_of_order[3] = out_of_order[3], out_of_order[2]
        variants.append(out_of_order)
        mixed = copy.deepcopy(events)
        mixed[4]["session_id"] = "other-session"
        variants.append(mixed)
        for variant in variants:
            with self.subTest(), self.assertRaises(ValueError):
                contracts.require_event_stream(variant)

    def test_generated_types_are_current(self):
        subprocess.run([sys.executable, "scripts/generate_contract_types.py", "--check"],
                       check=True)

    def test_contract_cli_is_checkout_independent(self):
        for name, version in (("evaluation", "table.evaluation/1.0"),
                              ("event", "table.event/1.0")):
            result = subprocess.run(
                [sys.executable, "-m", "tablekit.cli", "contract", name],
                cwd=os.path.dirname(os.path.dirname(__file__)), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            self.assertEqual(json.loads(result.stdout)["properties"]["schema_version"]["const"], version)


if __name__ == "__main__":
    unittest.main()
