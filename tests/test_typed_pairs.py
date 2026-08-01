"""Adversarial coverage for typed obligations and source-safe ingestion."""

import os
import re
import tempfile
import threading
import unittest

from tablekit import cli, ingest, pairs, report, ux
from tablekit.config import ConfigError, TableConfig
from tablekit.events import Ledger, SchemaError, make


BASE = {
    "name": "Typed table",
    "gm": {"id": "gm", "display": "GM"},
    "seats": [
        {"id": "rowan", "display": "Rowan", "kind": "human",
         "aliases": ["Ro"]},
        {"id": "vesh", "display": "Vesh", "kind": "agent",
         "mention": "<@42>"},
    ],
}


def human(mid, text="I answer", author="Rowan", **extra):
    return {"id": mid, "author": author, "author_id": f"user-{author}",
            "is_bot": False, "content": text, **extra}


_DIGIT = {"0": "zero", "1": "one", "2": "two", "3": "three",
          "4": "four", "5": "five", "6": "six", "7": "seven",
          "8": "eight", "9": "nine"}


def relay(mid, actor="Rowan", total=17, breakdown="1d20 + 3 -> (14) + 3",
          **extra):
    keycap = "".join(f":{_DIGIT[d]}:" for d in str(total))
    return {"id": mid, "author": "Beyond 20", "author_id": "relay-1",
            "is_bot": True, "content": "",
            "embeds": [{"title": f"{actor}: Perception",
                        "fields": [{"name": keycap, "value": breakdown}]}],
            **extra}


class Harness(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.ledger = Ledger(os.path.join(self.directory, "session.jsonl"))
        self.cfg = TableConfig(dict(BASE, data_dir=self.directory))

    def relay_config(self, aliases=None):
        seats = [{"id": "rowan", "display": "Rowan", "kind": "human",
                  "aliases": aliases or []}]
        return TableConfig({"name": "Relay table", "data_dir": self.directory,
                            "gm": {"id": "gm", "display": "GM"},
                            "seats": seats,
                            "transport": {"roll_relay_bots": ["Beyond20"]}})


class TestOneResolution(Harness):
    def test_unique_compatible_pair_requires_explicit_correlation(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan")
        ingest.ingest_message(self.cfg, self.ledger, human("m-1"))
        self.assertEqual([p["id"] for p in pairs.open_now(self.ledger)],
                         ["cue-1"])
        self.assertEqual(self.ledger.read(etype="out.close"), [])
        [receipt] = self.ledger.read(etype="qa.inbound")
        [route] = self.ledger.read(etype="qa.route")
        self.assertEqual(receipt["source_id"], "m-1")
        self.assertEqual((route["status"], route["reason"]),
                         ("quarantined", "missing_correlation"))
        self.assertEqual(route["pair_ids"], ["cue-1"])

    def test_two_compatible_pairs_fail_closed(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan")
        pairs.open_pair(self.ledger, "checkin", "check-1", seat="rowan")
        ingest.ingest_message(self.cfg, self.ledger, human("m-2"))
        self.assertEqual({p["id"] for p in pairs.open_now(self.ledger)},
                         {"cue-1", "check-1"})
        self.assertEqual(self.ledger.read(etype="qa.route")[-1]["reason"],
                         "missing_correlation")

    def test_explicit_pair_closes_only_that_pair(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan")
        pairs.open_pair(self.ledger, "checkin", "check-1", seat="rowan")
        ingest.ingest_message(
            self.cfg, self.ledger, human("m-3", pair_id="check-1"))
        self.assertEqual([p["id"] for p in pairs.open_now(self.ledger)], ["cue-1"])
        [closed] = self.ledger.read(etype="out.close")
        self.assertEqual((closed["id"], closed["pair"], closed["outcome"]),
                         ("check-1", "checkin", "returned"))

    def test_native_correlation_id_closes_the_named_pair(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan")
        ingest.ingest_message(
            self.cfg, self.ledger,
            human("m-native-correlation", correlation_id="cue-1"))
        self.assertEqual(pairs.open_now(self.ledger), [])
        [closed] = self.ledger.read(etype="out.close")
        self.assertEqual((closed["id"], closed["outcome"]), ("cue-1", "taken"))

    def test_explicit_cross_seat_pair_is_refused(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="vesh")
        ingest.ingest_message(
            self.cfg, self.ledger, human("m-4", pair_id="cue-1"))
        self.assertEqual(len(pairs.open_now(self.ledger)), 1)
        self.assertEqual(self.ledger.read(etype="qa.route")[-1]["reason"],
                         "actor_mismatch")

    def test_explicit_incompatible_kind_is_refused(self):
        pairs.open_pair(self.ledger, "roll", "roll-1", seat="rowan")
        ingest.ingest_message(
            self.cfg, self.ledger, human("m-5", "14", pair_id="roll-1"))
        self.assertEqual(len(pairs.open_now(self.ledger, "roll")), 1)
        self.assertEqual(self.ledger.read(etype="qa.route")[-1]["reason"],
                         "incompatible_pair")

    def test_two_correlation_fields_are_refused_even_when_they_agree(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan")
        ingest.ingest_message(
            self.cfg, self.ledger,
            human("m-double", pair_id="cue-1", correlation_id="cue-1"))
        self.assertEqual(len(pairs.open_now(self.ledger)), 1)
        self.assertEqual(self.ledger.read(etype="qa.route")[-1]["reason"],
                         "duplicate_correlation_fields")

    def test_empty_inbound_is_not_an_acknowledgement(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan")
        ingest.ingest_message(self.cfg, self.ledger, human("m-6", "   "))
        self.assertEqual(len(pairs.open_now(self.ledger)), 1)
        self.assertEqual(self.ledger.read(etype="qa.route")[-1]["reason"],
                         "blank_content")

    def test_manual_cli_refuses_missing_correlation_and_accepts_explicit_pair(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan")
        pairs.open_pair(self.ledger, "checkin", "check-1", seat="rowan")
        common = ["--ledger", self.ledger.path]
        refused = cli.main(["inbound", "--seat", "rowan", "--text", "yes"]
                           + common)
        self.assertEqual(refused, 2)
        self.assertEqual(len(pairs.open_now(self.ledger)), 2)
        refused_route = self.ledger.read(etype="qa.route")[-1]
        self.assertEqual(refused_route["reason"], "missing_correlation")
        self.assertEqual(refused_route["pair_ids"], ["check-1", "cue-1"])
        accepted = cli.main(["inbound", "--seat", "rowan", "--text", "yes",
                             "--pair", "cue-1"] + common)
        self.assertEqual(accepted, 0)
        self.assertEqual([p["id"] for p in pairs.open_now(self.ledger)],
                         ["check-1"])


class TestIdentityBoundaries(Harness):
    def test_exact_normalized_alias_matches(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan")
        ingest.ingest_message(self.cfg, self.ledger,
                              human("id-1", author="  rO  ", pair_id="cue-1"))
        self.assertEqual(pairs.open_now(self.ledger), [])

    def test_substring_is_not_an_identity(self):
        cfg = TableConfig({"name": "Names", "data_dir": self.directory,
                           "seats": [{"id": "will", "display": "Will"}]})
        self.assertIsNone(cfg.seat("William"))
        ingest.ingest_message(cfg, self.ledger,
                              human("id-2", author="William"))
        self.assertEqual(self.ledger.read(etype="qa.inbound")[-1]["seat"],
                         "unknown")

    def test_nfkc_casefold_collision_is_rejected(self):
        data = {"name": "Names", "seats": [
            {"id": "one", "display": "Alice"},
            {"id": "two", "display": "ＡＬＩＣＥ"},
        ]}
        with self.assertRaises(ConfigError):
            TableConfig(data)

    def test_duplicate_normalized_alias_on_one_seat_is_rejected(self):
        data = {"name": "Names", "seats": [
            {"id": "one", "display": "One", "aliases": ["Ash", "ＡＳＨ"]},
        ]}
        with self.assertRaises(ConfigError):
            TableConfig(data)

    def test_alias_that_duplicates_its_seat_display_is_rejected(self):
        data = {"name": "Names", "seats": [
            {"id": "one", "display": "Alice", "aliases": ["ＡＬＩＣＥ"]},
        ]}
        with self.assertRaises(ConfigError):
            TableConfig(data)

    def test_duplicate_normalized_relay_identities_are_rejected(self):
        data = {**BASE,
                "transport": {"roll_relay_bots": ["Beyond 20", "beyond20"]}}
        with self.assertRaises(ConfigError):
            TableConfig(data)

    def test_unknown_and_role_mismatch_remain_unknown(self):
        for msg in (human("id-3", author="Guest"),
                    {**human("id-4"), "is_bot": True}):
            ingest.ingest_message(self.cfg, self.ledger, msg)
        receipts = self.ledger.read(etype="qa.inbound")
        self.assertEqual([r["seat"] for r in receipts], ["unknown", "unknown"])
        self.assertEqual([r["reason"] for r in self.ledger.read(etype="qa.route")],
                         ["unknown_principal", "role_mismatch"])

    def test_agent_bot_role_and_native_principal_are_preserved(self):
        pairs.open_pair(self.ledger, "cue", "cue-agent", seat="vesh")
        ingest.ingest_message(
            self.cfg, self.ledger,
            {"id": "agent-1", "author": "Vesh", "author_id": "principal-42",
             "is_bot": True, "content": "I answer", "pair_id": "cue-agent"})
        [receipt] = self.ledger.read(etype="qa.inbound")
        self.assertEqual((receipt["seat"], receipt["principal_id"],
                          receipt["source_role"]),
                         ("vesh", "principal-42", "bot"))
        self.assertEqual(pairs.open_now(self.ledger), [])


class TestDurableReceipts(Harness):
    def test_native_ids_are_scoped_by_transport(self):
        discord = TableConfig({**BASE, "data_dir": self.directory,
                               "transport": {"kind": "discord"}})
        slack = TableConfig({**BASE, "data_dir": self.directory,
                             "transport": {"kind": "slack"}})
        msg = human("same-native-id", "hello")
        ingest.ingest_message(discord, self.ledger, msg)
        ingest.ingest_message(slack, self.ledger, msg)
        self.assertEqual(
            {(r["source"], r["source_id"])
             for r in self.ledger.read(etype="qa.inbound")},
            {("discord", "same-native-id"), ("slack", "same-native-id")})

    def test_same_native_id_cannot_be_replayed_with_new_meaning(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan")
        ingest.ingest_message(
            self.cfg, self.ledger,
            human("mutated", "first", pair_id="cue-1"))
        pairs.open_pair(self.ledger, "cue", "cue-2", seat="rowan")
        ingest.ingest_message(
            self.cfg, self.ledger,
            human("mutated", "changed", pair_id="cue-2"))
        self.assertEqual(len(self.ledger.read(etype="qa.inbound")), 1)
        self.assertEqual([p["id"] for p in pairs.open_now(self.ledger)], ["cue-2"])
        self.assertEqual(self.ledger.read(etype="qa.route")[-1]["reason"],
                         "source_payload_mismatch")

    def test_unresolved_quarantine_makes_an_adequate_report_actionable(self):
        for _ in range(3):
            self.ledger.append("ux.beat", words=5, chunks=1)
        self.ledger.append("qa.route", source="discord", source_id="q-1",
                           status="quarantined", reason="unknown_principal")
        built = report.build(self.ledger, self.cfg)
        self.assertEqual(report.exit_code(built), 1)

    def test_invalid_timestamp_is_receipted_and_deduplicated_before_rejection(self):
        msg = human("bad-time", ts="yesterday")
        for _ in range(3):
            ingest.ingest_message(self.cfg, self.ledger, msg)
        [receipt] = self.ledger.read(etype="qa.inbound")
        self.assertTrue(receipt["source_timestamp_invalid"])
        [route] = self.ledger.read(etype="qa.route")
        self.assertEqual((route["status"], route["reason"]),
                         ("quarantined", "invalid_timestamp"))

    def test_quarantined_relay_replay_has_one_receipt_and_disposition(self):
        cfg = self.relay_config()
        msg = relay("relay-bad", actor="Nobody")
        for _ in range(5):
            ingest.ingest_message(cfg, self.ledger, msg)
        self.assertEqual(len(self.ledger.read(etype="qa.inbound")), 1)
        self.assertEqual(len(self.ledger.read(etype="qa.route")), 1)
        self.assertEqual(self.ledger.read(etype="act"), [])

    def test_successful_relay_replay_does_not_repeat_effects(self):
        cfg = self.relay_config()
        pairs.open_pair(self.ledger, "roll", "roll-1", seat="rowan")
        msg = relay("relay-good", pair_id="roll-1")
        for _ in range(5):
            ingest.ingest_message(cfg, self.ledger, msg)
        self.assertEqual(len(self.ledger.read(etype="qa.inbound")), 1)
        self.assertEqual(len(self.ledger.read(etype="act")), 1)
        [closed] = self.ledger.read(etype="out.close")
        self.assertEqual((closed["id"], closed["roll_total"],
                          closed["roll_confidence"], closed["roll_provenance"]),
                         ("roll-1", 17, "source_observed", "relay_observed"))

    def test_concurrent_replay_has_one_receipt_and_one_resolution(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan")
        msg = human("concurrent-1", pair_id="cue-1")
        errors = []

        def worker():
            try:
                ingest.ingest_message(self.cfg, self.ledger, msg)
            except Exception as error:  # thread failures must fail this test
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.ledger.read(etype="qa.inbound")), 1)
        self.assertEqual(len(self.ledger.read(etype="out.close")), 1)
        self.assertNotIn("quarantined",
                         {r["status"] for r in self.ledger.read(etype="qa.route")})

    def test_unknown_principal_can_be_repaired_by_new_exact_config(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan")
        msg = human("repair-user", author="Guest", pair_id="cue-1")
        ingest.ingest_message(self.cfg, self.ledger, msg)
        repaired = TableConfig({**BASE, "data_dir": self.directory,
                                "seats": [
                                    {"id": "rowan", "display": "Rowan",
                                     "aliases": ["Guest"]},
                                    BASE["seats"][1],
                                ]})
        ingest.ingest_message(repaired, self.ledger, msg)
        self.assertEqual(len(self.ledger.read(etype="qa.inbound")), 1)
        self.assertEqual(len(self.ledger.read(etype="out.close")), 1)
        self.assertEqual([r["status"] for r in self.ledger.read(etype="qa.route")],
                         ["quarantined", "routed"])
        self.assertEqual(ux.seat_stats(self.ledger, repaired)["seats"]["rowan"]["lines"],
                         1)
        stats = ux.transport_stats(self.ledger)
        self.assertEqual((stats["routing_quarantines"],
                          stats["routing_quarantine_events"]), (0, 1))

    def test_relay_attribution_can_be_repaired_by_exact_alias(self):
        cfg = self.relay_config()
        pairs.open_pair(self.ledger, "roll", "roll-1", seat="rowan")
        msg = relay("repair-relay", actor="Ash", pair_id="roll-1")
        ingest.ingest_message(cfg, self.ledger, msg)
        ingest.ingest_message(self.relay_config(["Ash"]), self.ledger, msg)
        self.assertEqual(len(self.ledger.read(etype="qa.inbound")), 1)
        self.assertEqual(len(self.ledger.read(etype="act")), 1)
        self.assertEqual(len(self.ledger.read(etype="out.close")), 1)
        self.assertEqual([r["status"] for r in self.ledger.read(etype="qa.route")],
                         ["quarantined", "routed"])


class TestRelayValidation(Harness):
    def test_conflicting_structured_actor_fields_are_ambiguous(self):
        cfg = TableConfig({**BASE, "data_dir": self.directory,
                           "transport": {"roll_relay_bots": ["Beyond20"]}})
        msg = relay("conflicting-actor")
        msg["embeds"][0]["author"] = {"name": "Vesh"}
        ingest.ingest_message(cfg, self.ledger, msg)
        self.assertEqual(self.ledger.read(etype="act"), [])
        self.assertEqual(self.ledger.read(etype="qa.route")[-1]["reason"],
                         "relay_attribution_ambiguous")

    def test_one_relay_result_does_not_close_two_rolls(self):
        cfg = self.relay_config()
        pairs.open_pair(self.ledger, "roll", "roll-1", seat="rowan")
        pairs.open_pair(self.ledger, "roll", "roll-2", seat="rowan")
        ingest.ingest_message(cfg, self.ledger, relay("ambiguous-roll"))
        self.assertEqual({p["id"] for p in pairs.open_now(self.ledger, "roll")},
                         {"roll-1", "roll-2"})
        self.assertEqual(self.ledger.read(etype="qa.route")[-1]["reason"],
                         "missing_correlation")

    def test_uncorrelated_relay_observation_does_not_close_unique_roll(self):
        cfg = self.relay_config()
        pairs.open_pair(self.ledger, "roll", "roll-1", seat="rowan")
        ingest.ingest_message(cfg, self.ledger, relay("uncorrelated-roll"))
        self.assertEqual([p["id"] for p in pairs.open_now(self.ledger, "roll")],
                         ["roll-1"])
        self.assertEqual(self.ledger.read(etype="out.close"), [])
        [act] = self.ledger.read(etype="act")
        [route] = self.ledger.read(etype="qa.route")
        self.assertEqual(act["source_id"], "uncorrelated-roll")
        self.assertEqual((route["status"], route["reason"]),
                         ("quarantined", "missing_correlation"))
        self.assertEqual(route["pair_ids"], ["roll-1"])

    def test_explicit_relay_correlation_closes_only_one_roll(self):
        cfg = self.relay_config()
        pairs.open_pair(self.ledger, "roll", "roll-1", seat="rowan")
        pairs.open_pair(self.ledger, "roll", "roll-2", seat="rowan")
        ingest.ingest_message(
            cfg, self.ledger, relay("explicit-roll", pair_id="roll-2"))
        self.assertEqual([p["id"] for p in pairs.open_now(self.ledger, "roll")],
                         ["roll-1"])
        [closed] = self.ledger.read(etype="out.close")
        self.assertEqual(closed["id"], "roll-2")

    def test_null_relay_has_no_act_or_resolution(self):
        cfg = self.relay_config()
        pairs.open_pair(self.ledger, "roll", "roll-1", seat="rowan")
        msg = {"id": "null-relay", "author": "Beyond20",
               "author_id": "relay-1", "is_bot": True, "content": "",
               "embeds": []}
        ingest.ingest_message(cfg, self.ledger, msg)
        self.assertEqual(self.ledger.read(etype="act"), [])
        self.assertEqual(len(pairs.open_now(self.ledger, "roll")), 1)
        self.assertEqual(self.ledger.read(etype="qa.route")[-1]["reason"],
                         "relay_evidence_missing")

    def test_attributed_relay_without_total_has_no_act_or_resolution(self):
        cfg = self.relay_config()
        pairs.open_pair(self.ledger, "roll", "roll-1", seat="rowan")
        msg = relay("null-total")
        msg["embeds"][0]["fields"][0]["name"] = "Result unavailable"
        ingest.ingest_message(cfg, self.ledger, msg)
        self.assertEqual(self.ledger.read(etype="act"), [])
        self.assertEqual(len(pairs.open_now(self.ledger, "roll")), 1)
        self.assertEqual(self.ledger.read(etype="qa.route")[-1]["reason"],
                         "relay_missing_total")

    def test_malformed_relay_shape_is_receipted_and_quarantined(self):
        cfg = self.relay_config()
        msg = {"id": "malformed-relay", "author": "Beyond20",
               "author_id": "relay-1", "is_bot": True, "content": "",
               "embeds": 42}
        ingest.ingest_message(cfg, self.ledger, msg)
        self.assertEqual(len(self.ledger.read(etype="qa.inbound")), 1)
        self.assertEqual(self.ledger.read(etype="qa.route")[-1]["reason"],
                         "relay_evidence_missing")

    def test_display_name_spoof_is_not_relay_authority(self):
        cfg = self.relay_config()
        pairs.open_pair(self.ledger, "roll", "roll-1", seat="rowan")
        msg = relay("spoof")
        msg["is_bot"] = False
        ingest.ingest_message(cfg, self.ledger, msg)
        self.assertEqual(self.ledger.read(etype="act"), [])
        self.assertEqual(len(pairs.open_now(self.ledger, "roll")), 1)
        self.assertEqual(self.ledger.read(etype="qa.route")[-1]["reason"],
                         "relay_identity_unverified")

    def test_impossible_d20_natural_is_quarantined(self):
        cfg = self.relay_config()
        pairs.open_pair(self.ledger, "roll", "roll-1", seat="rowan")
        msg = relay("nat-21", total=21, breakdown="natural 21")
        ingest.ingest_message(cfg, self.ledger, msg)
        self.assertEqual(self.ledger.read(etype="act"), [])
        self.assertEqual(len(pairs.open_now(self.ledger, "roll")), 1)
        self.assertEqual(self.ledger.read(etype="qa.route")[-1]["reason"],
                         "natural_out_of_range")

    def test_natural_must_fit_declared_die(self):
        parsed = ingest.parse_relay_roll(
            relay("d6-7", total=7, breakdown="1d6 -> (7)"))
        self.assertFalse(parsed["valid"])
        self.assertEqual(parsed["validation"], "natural_out_of_range")

    def test_plausible_natural_for_declared_die_is_accepted(self):
        parsed = ingest.parse_relay_roll(
            relay("d100", total=100, breakdown="1d100 -> (100)"))
        self.assertTrue(parsed["valid"])
        self.assertEqual((parsed["die"], parsed["natural"]),
                         ({"count": 1, "sides": 100}, 100))


class TestProseIsAdvisory(Harness):
    def _open(self):
        pairs.open_pair(self.ledger, "roll", "roll-1", seat="rowan")

    def test_clear_total_is_only_advisory(self):
        self._open()
        ingest.ingest_message(self.cfg, self.ledger, human("prose-1", "14"))
        self.assertEqual(len(pairs.open_now(self.ledger, "roll")), 1)
        self.assertEqual(self.ledger.read(etype="act"), [])
        [finding] = self.ledger.read(etype="qc.finding")
        self.assertEqual(finding["provenance"], "prose_advisory")

    def test_damage_healing_and_non_roll_units_are_not_candidates(self):
        for i, text in enumerate(("14 damage", "I heal 8 hp", "move 30 feet")):
            ledger = Ledger(os.path.join(self.directory, f"nonroll-{i}.jsonl"))
            pairs.open_pair(ledger, "roll", f"roll-{i}", seat="rowan")
            ingest.ingest_message(self.cfg, ledger, human(f"nonroll-{i}", text))
            self.assertEqual(ledger.read(etype="qc.finding"), [])
            self.assertEqual(len(pairs.open_now(ledger, "roll")), 1)

    def test_impossible_prose_natural_is_visible_but_not_consumed(self):
        self._open()
        ingest.ingest_message(self.cfg, self.ledger,
                              human("prose-nat", "natural 99"))
        [finding] = self.ledger.read(etype="qc.finding")
        self.assertEqual((finding["confidence"], finding["candidate_total"]),
                         ("invalid", 99))
        self.assertEqual(len(pairs.open_now(self.ledger, "roll")), 1)


class TestPairLifecycle(Harness):
    def test_generated_ids_are_unique_and_not_short_random_suffixes(self):
        ids = {pairs.new_id("cue", 1000) for _ in range(2000)}
        self.assertEqual(len(ids), 2000)
        self.assertTrue(all(re.fullmatch(r"cue-1000000-[0-9a-f]{32}", pid)
                            for pid in ids))

    def test_duplicate_open_is_a_typed_failure(self):
        pairs.open_pair(self.ledger, "cue", "same", seat="rowan")
        with self.assertRaises(pairs.PairError) as error:
            pairs.open_pair(self.ledger, "cue", "same", seat="rowan")
        self.assertEqual(error.exception.code, "duplicate_pair_id")

    def test_orphan_close_is_refused(self):
        with self.assertRaises(pairs.PairError) as error:
            pairs.close_pair(self.ledger, "cue", "missing", "taken")
        self.assertEqual(error.exception.code, "orphan_close")

    def test_kind_and_outcome_are_not_interchangeable(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan")
        with self.assertRaises(pairs.PairError) as kind_error:
            pairs.close_pair(self.ledger, "roll", "cue-1", "consumed")
        self.assertEqual(kind_error.exception.code, "pair_kind_mismatch")
        with self.assertRaises(pairs.PairError) as outcome_error:
            pairs.close_pair(self.ledger, "cue", "cue-1", "consumed")
        self.assertEqual(outcome_error.exception.code, "invalid_pair_outcome")

    def test_resolution_cannot_precede_open(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan", ts=100)
        with self.assertRaises(pairs.PairError) as error:
            pairs.close_pair(self.ledger, "cue", "cue-1", "taken", ts=99)
        self.assertEqual(error.exception.code, "resolution_precedes_open")

    def test_resolution_cannot_replace_immutable_opener_time(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan", ts=100)
        with self.assertRaises(pairs.PairError) as error:
            pairs.close_pair(self.ledger, "cue", "cue-1", "taken", ts=110,
                             opened_ts=99)
        self.assertEqual(error.exception.code, "opener_mismatch")

    def test_second_resolution_is_refused(self):
        pairs.open_pair(self.ledger, "cue", "cue-1", seat="rowan")
        pairs.close_pair(self.ledger, "cue", "cue-1", "taken")
        with self.assertRaises(pairs.PairError) as error:
            pairs.close_pair(self.ledger, "cue", "cue-1", "ignored")
        self.assertEqual(error.exception.code, "pair_already_closed")

    def test_reordered_raw_ledger_is_not_folded_into_state(self):
        self.ledger.append("out.close", pair="cue", id="cue-1", outcome="taken")
        self.ledger.append("out.open", pair="cue", id="cue-1", seat="rowan")
        with self.assertRaises(pairs.PairError) as error:
            pairs.pairs(self.ledger)
        self.assertEqual(error.exception.code, "orphan_close")

    def test_schema_refuses_cross_kind_outcome_and_unknown_route_status(self):
        with self.assertRaises(SchemaError):
            make("out.close", pair="cue", id="x", outcome="consumed")
        with self.assertRaises(SchemaError):
            make("qa.route", source="discord", status="maybe", reason="x")

    def test_atomic_append_once_has_one_owner(self):
        outcomes = []

        def worker():
            outcomes.append(self.ledger.append_once(
                "qa.inbound", {"source": "discord", "source_id": "once"},
                source="discord", source_id="once", seat="rowan", chars=2)[1])

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(len(self.ledger.read(etype="qa.inbound")), 1)

    def test_append_once_recovers_after_a_crash_truncated_final_row(self):
        with open(self.ledger.path, "wb") as stream:
            stream.write(b'{"ts":1,"type":"qa.inbound"')
        self.ledger.append_once(
            "qa.inbound", {"source": "discord", "source_id": "after-crash"},
            source="discord", source_id="after-crash", seat="rowan", chars=2)
        self.assertEqual(len(self.ledger.read(etype="qa.inbound")), 1)
        self.assertEqual([row["type"] for row in self.ledger.read()],
                         ["_malformed", "qa.inbound"])


if __name__ == "__main__":
    unittest.main()
