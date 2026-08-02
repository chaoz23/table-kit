import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

from jsonschema import Draft202012Validator, FormatChecker

from tablekit import contracts
from tablekit.cli import main
from tablekit.events import Ledger
from tablekit.legacy_events import LegacyMigrationError, migrate_ledger


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


class LegacyMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = Draft202012Validator(
            contracts.schema("event"), format_checker=FormatChecker())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_path = os.path.join(self.tmp.name, "legacy.jsonl")
        self.ledger = Ledger(self.ledger_path)

    def tearDown(self):
        self.tmp.cleanup()

    def assert_valid_stream(self, migration):
        for event in migration.events:
            self.validator.validate(event)
        contracts.require_event_stream(migration.events)
        self.assertTrue(all(event["source"]["attestation"] == "self_attested"
                            for event in migration.events))

    def test_play_rows_map_deterministically_without_granting_authority(self):
        self.ledger.append("turn", ts=1, actor="Rowan")
        self.ledger.append("act", ts=2, actor="Rowan", text="casts Shield",
                           source="discord", source_id="message-2")
        self.ledger.append("event", ts=3, actor="GM", text="The ward flares.",
                           correlation_id="shield-1")
        first = migrate_ledger(self.ledger_path, "campaign", "session")
        second = migrate_ledger(self.ledger_path, "campaign", "session")
        self.assert_valid_stream(first)
        self.assertEqual(first.events, second.events)
        self.assertEqual([item["event_type"] for item in first.events],
                         ["turn.started", "action.declared", "narration.observed"])
        self.assertEqual(first.events[1]["source"]["native_id"], "message-2")
        self.assertNotEqual(first.events[1]["event_id"], "message-2")
        self.assertEqual(first.events[2]["payload"]["resolves_obligation_ids"],
                         ["shield-1"])

    def test_roll_pair_keeps_exact_obligation_chain(self):
        self.ledger.append("out.open", ts=1, pair="roll", id="roll-pair-1")
        self.ledger.append("act", ts=2, actor="Vesh", text="Attack: 19",
                           roll_total=19, roll_natural=14, roll_check="attack",
                           source="discord", source_id="message-roll")
        self.ledger.append("out.close", ts=3, pair="roll", id="roll-pair-1",
                           outcome="consumed", source="discord",
                           source_id="message-roll")
        migration = migrate_ledger(self.ledger_path, "campaign", "session")
        self.assert_valid_stream(migration)
        roll, obligation, narration = migration.events
        self.assertEqual(obligation["causation_id"], roll["event_id"])
        self.assertEqual(narration["causation_id"], obligation["event_id"])
        self.assertEqual(narration["payload"]["resolves_obligation_ids"],
                         ["roll-pair-1"])
        self.assertEqual(migration.skipped[0]["reason"],
                         "represented_by_roll_obligation")

    def test_engine_narration_ack_maps_to_exact_derived_resolution(self):
        event = self.ledger.append(
            "event", ts=1, actor="engine", text="door opens",
            engine_log_index=1, engine_log_fingerprint="a" * 64,
            engine_source_id="engine-1")
        self.ledger.append(
            "qc.mark", ts=2, narrated_through=1, mark_kind="narration_ack",
            ack_engine_log_index=1, ack_engine_log_fingerprint="a" * 64,
            engine_source_id="engine-1")
        migration = migrate_ledger(self.ledger_path, "campaign", "session")
        self.assert_valid_stream(migration)
        obligation, narration = migration.events
        self.assertEqual(obligation["event_type"], "narration.obligation")
        self.assertEqual(narration["payload"]["resolves_obligation_ids"],
                         [obligation["payload"]["obligation_id"]])
        self.assertEqual(narration["provenance"], "derived")
        self.assertEqual(event["engine_log_index"], 1)

    def test_unprojectable_telemetry_is_counted_not_silently_dropped(self):
        self.ledger.append("event", ts=1, actor="GM", text="Begin.")
        self.ledger.append("qa.delta", ts=2, topic="latency", detail="look later")
        migration = migrate_ledger(self.ledger_path, "campaign", "session")
        self.assertEqual(migration.summary()["input_count"], 2)
        self.assertEqual(migration.summary()["compatible_count"], 1)
        self.assertEqual(migration.summary()["skipped_count"], 1)
        self.assertEqual(migration.skipped[0]["type"], "qa.delta")

    def test_redacted_inbound_turn_and_partial_delivery_remain_explicit(self):
        self.ledger.append("qa.inbound", ts=1, seat="Rowan", chars=12,
                           source="discord", source_id="message-1")
        self.ledger.append("ux.turn", ts=2, seat="Rowan", wait_s=4)
        self.ledger.append("qa.post.partial", ts=3, operation_id="post-1",
                           chunks_sent=1, chunks_planned=2, error="timeout")
        migration = migrate_ledger(self.ledger_path, "campaign", "session")
        self.assert_valid_stream(migration)
        message, turn, gap = migration.events
        self.assertEqual(message["payload"],
                         {"content": "", "content_redacted": True})
        self.assertEqual(message["source"]["native_id"], "message-1")
        self.assertEqual(turn["payload"]["actor_id"], "Rowan")
        self.assertEqual(gap["event_type"], "transport.gap")
        self.assertEqual(gap["payload"]["observed_sequence"], 1)

    def test_malformed_and_zero_compatible_ledgers_are_refused(self):
        with open(self.ledger_path, "wb") as stream:
            stream.write(b"not-json\n")
        with self.assertRaises(LegacyMigrationError) as caught:
            migrate_ledger(self.ledger_path, "campaign", "session")
        self.assertEqual(caught.exception.code,
                         "legacy_migration.malformed_ledger")

        other = os.path.join(self.tmp.name, "telemetry.jsonl")
        Ledger(other).append("qa.delta", ts=1, topic="x", detail="y")
        with self.assertRaises(LegacyMigrationError) as caught:
            migrate_ledger(other, "campaign", "session")
        self.assertEqual(caught.exception.code,
                         "legacy_migration.zero_compatible")

    def test_cli_writes_separate_jsonl_and_machine_summary(self):
        self.ledger.append("event", ts=1, actor="GM", text="Begin.")
        output = os.path.join(self.tmp.name, "events.jsonl")
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(["migrate-events", "--ledger", self.ledger_path,
                         "--campaign", "campaign", "--session", "session",
                         "--out", output])
        self.assertEqual(code, 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["authority_status"], "self_attested")
        with open(output, encoding="utf-8") as stream:
            migrated = [json.loads(line) for line in stream if line.strip()]
        self.assertEqual(len(migrated), 1)
        self.validator.validate(migrated[0])
        with open(self.ledger_path, encoding="utf-8") as stream:
            self.assertEqual(json.loads(stream.readline())["type"], "event")

    def test_cli_refuses_overwriting_source_ledger(self):
        self.ledger.append("event", ts=1, actor="GM", text="Begin.")
        stderr = StringIO()
        from contextlib import redirect_stderr
        with redirect_stderr(stderr):
            code = main(["migrate-events", "--ledger", self.ledger_path,
                         "--campaign", "campaign", "--session", "session",
                         "--out", self.ledger_path])
        self.assertEqual(code, 2)
        self.assertIn("must not replace", stderr.getvalue())

    def test_cli_migration_failure_is_machine_readable(self):
        self.ledger.append("qa.delta", ts=1, topic="x", detail="y")
        output = os.path.join(self.tmp.name, "events.jsonl")
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(["migrate-events", "--ledger", self.ledger_path,
                         "--campaign", "campaign", "--session", "session",
                         "--out", output])
        self.assertEqual(code, 2)
        refusal = json.loads(stdout.getvalue())
        self.assertEqual(refusal["status"], "refused")
        self.assertEqual(refusal["error"]["code"],
                         "legacy_migration.zero_compatible")
        self.assertFalse(os.path.exists(output))


if __name__ == "__main__":
    unittest.main()
