"""Tests for table-kit.

Written with unittest rather than bare pytest functions on purpose: a sibling
project once had a whole test module silently collecting zero tests because
bare functions are invisible to `unittest discover`, and nobody noticed for two
releases. Everything here runs under either runner.
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tablekit import cli, detector, pairs, report, ux, uxr  # noqa: E402
from tablekit.config import ConfigError, TableConfig  # noqa: E402
from tablekit.events import Ledger, SchemaError, make, validate  # noqa: E402


def cfg_dict(**over):
    d = {
        "name": "Test Table",
        "data_dir": "/tmp/tk-test",
        "gm": {"id": "gm", "display": "GM"},
        "seats": [
            {"id": "rowan", "display": "Rowan", "kind": "human", "aliases": ["ro"]},
            {"id": "vesh", "display": "Vesh", "kind": "agent",
             "mention": "<@123>"},
        ],
    }
    d.update(over)
    return d


class TempLedger(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.led = Ledger(os.path.join(self.dir, "s.jsonl"))
        self.cfg = TableConfig(cfg_dict())


# ---------------------------------------------------------------- events


class TestEvents(TempLedger):
    def test_validate_rejects_unknown_type(self):
        with self.assertRaises(SchemaError):
            make("ux.vibes", seat="rowan")

    def test_validate_rejects_missing_required_key(self):
        with self.assertRaises(SchemaError):
            make("uxr.marker", seat="rowan")  # no marker

    def test_validate_rejects_unknown_pair_kind(self):
        with self.assertRaises(SchemaError):
            make("out.open", pair="vibes", id="x")

    def test_validate_rejects_unknown_outcome(self):
        with self.assertRaises(SchemaError):
            make("out.close", pair="cue", id="x", outcome="sort of")

    def test_extra_keys_allowed(self):
        rec = make("ux.beat", words=10, chunks=1, house_rule="anything")
        self.assertEqual(rec["house_rule"], "anything")

    def test_append_and_read_roundtrip(self):
        self.led.append("ux.beat", words=5, chunks=1)
        self.led.append("uxr.marker", seat="rowan", marker="yes")
        self.assertEqual(len(self.led.read()), 2)
        self.assertEqual(len(self.led.read(lane="uxr")), 1)

    def test_play_events_are_dmcheck_shaped(self):
        """The play lane must stay in the shape dmcheck's ledger reader wants."""
        rec = self.led.append("act", actor="Rowan", text="swings")
        self.assertEqual(set(("ts", "type", "actor", "text")) - set(rec), set())
        self.assertIn(rec["type"], ("turn", "act", "event"))

    def test_malformed_line_is_surfaced_not_dropped(self):
        self.led.append("ux.beat", words=1, chunks=1)
        with open(self.led.path, "a") as f:
            f.write("{not json\n")
        rows = self.led.read()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["type"], "_malformed")

    def test_current_beat_counts_beats(self):
        self.assertEqual(self.led.current_beat(), 0)
        self.led.append("ux.beat", words=1, chunks=1)
        self.led.append("ux.beat", words=1, chunks=1)
        self.assertEqual(self.led.current_beat(), 2)


# ---------------------------------------------------------------- config


class TestConfig(unittest.TestCase):
    def test_agent_seat_without_mention_is_refused(self):
        d = cfg_dict()
        del d["seats"][1]["mention"]
        with self.assertRaises(ConfigError) as e:
            TableConfig(d)
        self.assertIn("mention", str(e.exception))

    def test_human_seat_needs_no_mention(self):
        TableConfig(cfg_dict())  # rowan has none; must not raise

    def test_seat_lookup_by_alias_and_display(self):
        c = TableConfig(cfg_dict())
        self.assertEqual(c.seat("ro").id, "rowan")
        self.assertEqual(c.seat("Rowan").id, "rowan")
        self.assertIsNone(c.seat("nobody"))

    def test_mention_check_flags_missing_mention_for_agent(self):
        c = TableConfig(cfg_dict())
        problem = c.mention_check("Vesh, what do you do?", c.seat("vesh"))
        self.assertIsNotNone(problem)
        self.assertIsNone(
            c.mention_check("<@123> Vesh, what do you do?", c.seat("vesh")))

    def test_mention_check_never_flags_humans(self):
        c = TableConfig(cfg_dict())
        self.assertIsNone(c.mention_check("Rowan, you're up", c.seat("rowan")))

    def test_token_comes_from_env_only(self):
        c = TableConfig(cfg_dict(transport={"token_env": "TK_TEST_TOKEN"}))
        os.environ.pop("TK_TEST_TOKEN", None)
        with self.assertRaises(ConfigError):
            c.token()
        os.environ["TK_TEST_TOKEN"] = "abc"
        self.assertEqual(c.token(), "abc")
        del os.environ["TK_TEST_TOKEN"]


# ------------------------------------------------------------------ uxr


class TestUXR(unittest.TestCase):
    def test_parses_bare_marker(self):
        self.assertEqual(uxr.parse("!yes")[0]["marker"], "yes")

    def test_parses_marker_mid_sentence_with_note(self):
        got = uxr.parse("I move up !huh what's a gorse")
        self.assertEqual(got[0]["marker"], "huh")
        self.assertEqual(got[0]["note"], "what's a gorse")

    def test_parses_multiple_markers(self):
        got = uxr.parse("!yes\nalso !mine")
        self.assertEqual([g["marker"] for g in got], ["yes", "mine"])

    def test_case_insensitive(self):
        self.assertEqual(uxr.parse("!WAIT")[0]["marker"], "wait")

    def test_ignores_non_markers(self):
        self.assertEqual(uxr.parse("that's amazing!! no marker here"), [])
        self.assertEqual(uxr.parse("!wow"), [])

    def test_strip_removes_markers(self):
        self.assertEqual(uxr.strip("I draw my blade !yes"), "I draw my blade")

    def test_record_anchors_to_current_beat(self):
        d = tempfile.mkdtemp()
        led = Ledger(os.path.join(d, "s.jsonl"))
        led.append("ux.beat", words=3, chunks=1)
        led.append("ux.beat", words=3, chunks=1)
        [ev] = uxr.record(led, "rowan", "!drag")
        self.assertEqual(ev["beat"], 2)

    def test_stories_never_invent_a_cause(self):
        st = uxr.stories([{"seat": "rowan", "marker": "huh", "beat": 4}])
        self.assertEqual(st[0]["cause"], "not stated")
        self.assertIsNone(st[0]["said"])

    def test_followups_skip_markers_that_stated_their_cause(self):
        ms = [{"seat": "rowan", "marker": "huh", "beat": 1, "note": "the word gorse"},
              {"seat": "vesh", "marker": "huh", "beat": 2}]
        f = uxr.followups(ms)
        self.assertEqual([x["seat"] for x in f], ["vesh"])

    def test_mine_marker_has_no_followup_question(self):
        self.assertIsNone(uxr.MARKERS["mine"]["ask"])


# ---------------------------------------------------------------- pairs


class TestPairs(TempLedger):
    def test_open_close_records_latency(self):
        t0 = time.time()
        pairs.open_pair(self.led, "cue", "c1", seat="rowan", ts=t0)
        pairs.close_pair(self.led, "cue", "c1", "taken", ts=t0 + 12, opened_ts=t0)
        p = pairs.pairs(self.led)["c1"]
        self.assertEqual(p["outcome"], "taken")
        self.assertAlmostEqual(p["latency_s"], 12.0, places=1)

    def test_open_now_excludes_closed(self):
        pairs.open_pair(self.led, "cue", "c1", seat="rowan")
        pairs.open_pair(self.led, "roll", "r1", seat="vesh")
        pairs.close_pair(self.led, "cue", "c1", "taken")
        self.assertEqual([p["id"] for p in pairs.open_now(self.led)], ["r1"])

    def test_sweep_expires_by_kind_with_named_outcome(self):
        t0 = time.time() - 1000
        pairs.open_pair(self.led, "roll", "r1", seat="rowan", ts=t0)
        pairs.open_pair(self.led, "cue", "c1", seat="rowan", ts=t0)
        pairs.sweep(self.led, ttls={"roll_ttl_s": 300, "cue_ttl_s": 300})
        got = pairs.pairs(self.led)
        self.assertEqual(got["r1"]["outcome"], "unconsumed")
        self.assertEqual(got["c1"]["outcome"], "expired")

    def test_sweep_leaves_fresh_pairs_alone(self):
        pairs.open_pair(self.led, "cue", "c1", seat="rowan")
        self.assertEqual(pairs.sweep(self.led, ttls={"cue_ttl_s": 300}), [])

    def test_ids_do_not_collide_within_one_second(self):
        """Two beats inside one second must not share a cue id — the second
        cue would take the first one's identity and closing one would close
        both, making cue-uptake quietly wrong rather than obviously broken."""
        t = time.time()
        n = 5000
        ids = {pairs.new_id("cue", t) for _ in range(n)}
        self.assertEqual(len(ids), n)

    def test_two_rapid_cues_stay_separable(self):
        t = time.time()
        for _ in range(2):
            pairs.open_pair(self.led, "cue", pairs.new_id("cue", t), seat="rowan", ts=t)
        self.assertEqual(len(pairs.open_now(self.led, "cue")), 2)
        first = pairs.open_now(self.led, "cue")[0]
        pairs.close_pair(self.led, "cue", first["id"], "taken")
        self.assertEqual(len(pairs.open_now(self.led, "cue")), 1)

    def test_summary_withholds_rate_below_floor(self):
        pairs.open_pair(self.led, "cue", "c1", seat="rowan")
        pairs.close_pair(self.led, "cue", "c1", "taken")
        s = pairs.summary(self.led, min_n=3)["cue"]
        self.assertIsNone(s["success_rate"])
        self.assertIn("too few", s["note"])

    def test_summary_states_rate_at_or_above_floor(self):
        for i in range(4):
            pairs.open_pair(self.led, "cue", f"c{i}", seat="rowan")
            pairs.close_pair(self.led, "cue", f"c{i}",
                             "taken" if i < 3 else "expired")
        s = pairs.summary(self.led, min_n=3)["cue"]
        self.assertEqual(s["success_rate"], 0.75)

    def test_unclosed_pairs_are_counted_not_dropped(self):
        pairs.open_pair(self.led, "cue", "c1", seat="rowan")
        pairs.open_pair(self.led, "cue", "c2", seat="rowan")
        pairs.close_pair(self.led, "cue", "c1", "taken")
        self.assertEqual(pairs.summary(self.led)["cue"]["still_open"], 1)


# ------------------------------------------------------------- detector


class TestDetector(TempLedger):
    def test_empty_session_produces_no_findings(self):
        self.assertEqual(detector.check(self.led, self.cfg), [])

    def test_silence_alone_is_not_an_accusation(self):
        """A GM who yields the floor looks exactly like one who has gone
        missing. Without an inbound message there is no evidence, so there is
        no finding."""
        self.led.append("ux.beat", words=10, chunks=1)
        checks = [f["check"] for f in detector.check(self.led, self.cfg)]
        self.assertNotIn("unanswered", checks)

    def test_unanswered_fires_only_with_an_inbound_after_the_beat(self):
        t0 = time.time()
        self.led.append("ux.beat", words=10, chunks=1, ts=t0)
        self.led.append("qa.inbound", seat="rowan", chars=20, ts=t0 + 5)
        checks = [f["check"] for f in detector.check(self.led, self.cfg, now=t0 + 6)]
        self.assertIn("unanswered", checks)

    def test_seat_quiet_is_a_defect_with_evidence(self):
        t0 = time.time() - 3600
        self.led.append("ux.beat", words=10, chunks=1, ts=t0)
        self.led.append("qa.inbound", seat="rowan", chars=5, ts=t0 + 1)
        found = [f for f in detector.check(self.led, self.cfg)
                 if f["check"] == "seat_quiet"]
        self.assertTrue(found)
        self.assertEqual(found[0]["severity"], "defect")
        self.assertTrue(found[0]["evidence"])

    def test_checked_on_seat_is_not_reported_quiet(self):
        t0 = time.time() - 3600
        self.led.append("ux.beat", words=10, chunks=1, ts=t0)
        for s in ("rowan", "vesh"):
            pairs.open_pair(self.led, "checkin", f"ci-{s}", seat=s)
        quiet = [f for f in detector.check(self.led, self.cfg)
                 if f["check"] == "seat_quiet"]
        self.assertEqual(quiet, [])

    def test_undeliverable_cue_is_a_defect(self):
        self.led.append("ux.beat", words=6, chunks=1, cued_seat="vesh",
                        text="Vesh, the door opens. What do you do?")
        found = [f for f in detector.check(self.led, self.cfg)
                 if f["check"] == "undeliverable_cue"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "defect")

    def test_cue_with_mention_is_clean(self):
        self.led.append("ux.beat", words=8, chunks=1, cued_seat="vesh",
                        text="<@123> Vesh, the door opens. What do you do?")
        self.assertEqual([f for f in detector.check(self.led, self.cfg)
                          if f["check"] == "undeliverable_cue"], [])

    def test_unconsumed_roll_is_a_defect_after_ttl(self):
        t0 = time.time() - 1000
        self.led.append("ux.beat", words=4, chunks=1, ts=t0)
        pairs.open_pair(self.led, "roll", "r1", seat="rowan", ts=t0)
        found = [f for f in detector.check(self.led, self.cfg)
                 if f["check"] == "roll_unconsumed"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "defect")

    def test_long_beat_is_attention_not_defect(self):
        self.led.append("ux.beat", words=400, chunks=1)
        found = [f for f in detector.check(self.led, self.cfg)
                 if f["check"] == "long_beat"]
        self.assertEqual(found[0]["severity"], "attention")

    def test_split_beat_is_attention(self):
        self.led.append("ux.beat", words=30, chunks=4)
        found = [f for f in detector.check(self.led, self.cfg)
                 if f["check"] == "split_beat"]
        self.assertEqual(found[0]["severity"], "attention")

    def test_unnarrated_needs_engine_state_and_does_not_guess(self):
        self.led.append("ux.beat", words=10, chunks=1)
        self.assertEqual([f for f in detector.check(self.led, self.cfg)
                          if f["check"] == "unnarrated"], [])
        found = [f for f in detector.check(self.led, self.cfg,
                                           state={"log_len": 5, "narrated_through": 2})
                 if f["check"] == "unnarrated"]
        self.assertEqual(len(found), 1)

    def test_undeliverable_cue_found_on_earlier_beats_too(self):
        """An undeliverable cue from twenty minutes ago is usually the reason
        a seat has gone quiet — reporting the silence without the cause sends
        the GM looking in the wrong place."""
        self.led.append("ux.beat", words=6, chunks=1, cued_seat="vesh",
                        text="Vesh, the door opens.")
        for _ in range(3):
            self.led.append("ux.beat", words=6, chunks=1)
        found = [f for f in detector.check(self.led, self.cfg)
                 if f["check"] == "undeliverable_cue"]
        self.assertEqual(len(found), 1)
        self.assertIn("beat 1", found[0]["detail"])

    def test_no_duplicate_undeliverable_findings_for_clean_cues(self):
        for _ in range(3):
            self.led.append("ux.beat", words=6, chunks=1, cued_seat="vesh",
                            text="<@123> Vesh, go.")
        self.assertEqual([f for f in detector.check(self.led, self.cfg)
                          if f["check"] == "undeliverable_cue"], [])

    def test_defects_sort_before_attention(self):
        self.led.append("ux.beat", words=400, chunks=4, cued_seat="vesh",
                        text="Vesh, go")
        f = detector.check(self.led, self.cfg)
        self.assertEqual(f[0]["severity"], "defect")


# --------------------------------------------------------------- report


class TestReport(TempLedger):
    def _session(self, beats=5, markers=(), defects=False):
        t0 = time.time() - 600
        for i in range(beats):
            self.led.append("ux.beat", words=20, chunks=1, ts=t0 + i * 10,
                            text=f"beat {i + 1}")
            self.led.append("qa.inbound", seat="rowan", chars=40, ts=t0 + i * 10 + 3)
            self.led.append("qa.post", ok=True, chars=100, chunks=1,
                            latency_ms=120, ts=t0 + i * 10)
        for seat, m in markers:
            self.led.append("uxr.marker", seat=seat, marker=m, beat=2)
        if defects:
            self.led.append("qc.finding", check="seat_quiet", detail="quiet",
                            severity="defect", seat="vesh")

    def test_short_session_is_refused_not_blessed(self):
        self.led.append("ux.beat", words=5, chunks=1)
        rep = report.build(self.led, self.cfg)
        self.assertFalse(rep["enough_data"])
        self.assertEqual(report.exit_code(rep), 2)
        self.assertIn("refusal", report.render(rep))

    def test_clean_session_exits_zero(self):
        self._session()
        rep = report.build(self.led, self.cfg)
        self.assertEqual(report.exit_code(rep), 0)

    def test_defects_exit_one(self):
        self._session(defects=True)
        rep = report.build(self.led, self.cfg)
        self.assertEqual(report.exit_code(rep), 1)

    def test_markers_below_floor_are_moments_not_patterns(self):
        self._session(markers=[("rowan", "huh"), ("rowan", "huh")])
        rep = report.build(self.led, self.cfg)
        self.assertEqual(rep["uxr"]["patterns"], [])
        self.assertEqual(rep["uxr"]["moments"][0]["count"], 2)
        self.assertIn("not a tendency", report.render(rep))

    def test_markers_at_floor_become_a_pattern(self):
        self._session(markers=[("rowan", "drag")] * 3)
        rep = report.build(self.led, self.cfg)
        self.assertEqual(rep["uxr"]["patterns"][0]["marker"], "drag")
        self.assertEqual(rep["uxr"]["moments"], [])

    def test_no_markers_says_so_rather_than_implying_no_friction(self):
        self._session()
        self.assertIn("absence of data", report.render(self.led and
                                                       report.build(self.led, self.cfg)))

    def test_report_contains_no_composite_score(self):
        self._session(markers=[("rowan", "yes")] * 3, defects=True)
        rep = report.build(self.led, self.cfg)
        flat = json.dumps(rep).lower()
        for banned in ('"score"', '"grade"', '"rating"', '"overall"'):
            self.assertNotIn(banned, flat)

    def test_stories_are_emitted_per_marker(self):
        self._session(markers=[("rowan", "wait"), ("vesh", "mine")])
        rep = report.build(self.led, self.cfg)
        self.assertEqual(len(rep["uxr"]["stories"]), 2)
        self.assertTrue(all(s["story"].startswith("As ")
                            for s in rep["uxr"]["stories"]))

    def test_render_is_plain_text_and_mentions_every_lane(self):
        self._session(markers=[("rowan", "yes")])
        text = report.render(report.build(self.led, self.cfg))
        for section in ("Defects", "From the seats", "Shape", "Machinery"):
            self.assertIn(section, text)


# ------------------------------------------------------------------- ux


class TestUX(TempLedger):
    def test_seat_stats_counts_lines_and_silence(self):
        t0 = time.time() - 3600
        self.led.append("ux.beat", words=10, chunks=1, ts=t0)
        self.led.append("qa.inbound", seat="rowan", chars=55, ts=t0 + 10)
        self.led.append("qa.inbound", seat="rowan", chars=55, ts=t0 + 3500)
        s = ux.seat_stats(self.led, self.cfg)
        self.assertEqual(s["seats"]["rowan"]["lines"], 2)
        # vesh never spoke across an hour of record.
        self.assertGreater(s["seats"]["vesh"]["longest_silence_s"], 3000)

    def test_silence_is_measured_against_the_record_not_the_clock(self):
        """A report must read the same tomorrow as it did at midnight."""
        t0 = time.time() - 7200
        self.led.append("ux.beat", words=10, chunks=1, ts=t0)
        self.led.append("qa.inbound", seat="rowan", chars=55, ts=t0 + 30)
        self.led.append("qa.inbound", seat="vesh", chars=55, ts=t0 + 60)
        s = ux.seat_stats(self.led, self.cfg)
        self.assertLess(s["seats"]["vesh"]["longest_silence_s"], 120)
        # ...unless the caller is live and asks about right now.
        live = ux.seat_stats(self.led, self.cfg, now=time.time())
        self.assertGreater(live["seats"]["vesh"]["longest_silence_s"], 7000)

    def test_gm_share_is_reported_not_judged(self):
        t0 = time.time() - 100
        self.led.append("ux.beat", words=90, chunks=1, ts=t0)
        self.led.append("qa.inbound", seat="rowan", chars=55, ts=t0 + 5)
        s = ux.seat_stats(self.led, self.cfg)
        self.assertGreater(s["gm_share_of_table"], 0.5)

    def test_transport_stats_counts_failures(self):
        self.led.append("qa.post", ok=True, chars=10, chunks=1, latency_ms=100)
        self.led.append("qa.post_failed", error="429")
        self.led.append("qa.listener", state="reconnect")
        t = ux.transport_stats(self.led)
        self.assertEqual((t["posts"], t["post_failures"],
                          t["listener_interruptions"]), (1, 1, 1))


# ------------------------------------------------------------------ cli


class TestCLI(TempLedger):
    def run_cli(self, *args):
        return cli.main(list(args) + ["--ledger", self.led.path])

    def test_help_and_version(self):
        self.assertEqual(cli.main([]), 0)
        self.assertEqual(cli.main(["--version"]), 0)

    def test_unknown_command_refuses(self):
        self.assertEqual(cli.main(["frobnicate"]), 2)

    def test_beat_then_inbound_closes_the_cue_pair(self):
        cfg_path = os.path.join(self.dir, "table.json")
        with open(cfg_path, "w") as f:
            json.dump(cfg_dict(data_dir=self.dir), f)
        common = ["--ledger", self.led.path, "--config", cfg_path]
        cli.main(["beat", "<@123> Vesh, the door opens.", "--cue", "vesh"] + common)
        self.assertEqual(len(pairs.open_now(self.led, "cue")), 1)
        cli.main(["inbound", "--seat", "vesh", "--text", "I open it !yes"] + common)
        self.assertEqual(pairs.open_now(self.led, "cue"), [])
        self.assertEqual(len(self.led.read(etype="uxr.marker")), 1)

    def test_beat_without_mention_exits_one(self):
        cfg_path = os.path.join(self.dir, "table.json")
        with open(cfg_path, "w") as f:
            json.dump(cfg_dict(data_dir=self.dir), f)
        code = cli.main(["beat", "Vesh, the door opens.", "--cue", "vesh",
                         "--ledger", self.led.path, "--config", cfg_path])
        self.assertEqual(code, 1)

    def test_roll_and_consumed_roundtrip(self):
        self.assertEqual(self.run_cli("roll", "--seat", "rowan", "climb"), 0)
        pid = pairs.open_now(self.led, "roll")[0]["id"]
        self.assertEqual(self.run_cli("consumed", pid), 0)
        self.assertEqual(pairs.open_now(self.led, "roll"), [])

    def test_consumed_unknown_pair_refuses(self):
        self.assertEqual(self.run_cli("consumed", "nope-1"), 2)

    def test_qc_exit_codes(self):
        self.assertEqual(self.run_cli("qc"), 0)
        self.led.append("ux.beat", words=900, chunks=9)
        self.assertEqual(self.run_cli("qc"), 1)

    def test_schema_and_markers_emit(self):
        self.assertEqual(self.run_cli("schema"), 0)
        self.assertEqual(self.run_cli("markers"), 0)

    def test_report_on_empty_refuses_with_two(self):
        self.assertEqual(self.run_cli("report"), 2)

    def test_init_refuses_to_overwrite(self):
        p = os.path.join(self.dir, "table.json")
        self.assertEqual(cli.main(["init", p]), 0)
        self.assertEqual(cli.main(["init", p]), 2)


if __name__ == "__main__":
    unittest.main()
