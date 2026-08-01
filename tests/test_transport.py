"""Transport tests — posting, ingestion, and the engine tap.

No network anywhere: `post()` takes a `send_fn` seam and everything else works
on the session file.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tablekit import engine, ingest, pairs, post  # noqa: E402
from tablekit.config import TableConfig  # noqa: E402
from tablekit.events import Ledger  # noqa: E402

CFG = {
    "name": "T",
    "gm": {"id": "gm", "display": "GM", "aliases": ["the gm bot"]},
    "seats": [
        {"id": "rowan", "display": "Rowan", "kind": "human"},
        {"id": "vesh", "display": "Vesh", "kind": "agent", "mention": "<@42>"},
    ],
}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.led = Ledger(os.path.join(self.dir, "s.jsonl"))
        self.cfg = TableConfig(dict(CFG, data_dir=self.dir))
        self.sent = []

    def send(self, _cfg, text):
        self.sent.append(text)
        return f"msg-{len(self.sent)}"


class TestSplit(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(post.split("hello"), ["hello"])

    def test_splits_on_paragraphs(self):
        text = ("a" * 1000) + "\n\n" + ("b" * 1000)
        chunks = post.split(text, limit=1200)
        self.assertEqual(len(chunks), 2)

    def test_never_splits_mid_word(self):
        text = " ".join(["word"] * 800)
        for c in post.split(text, limit=500):
            self.assertFalse(c.startswith("ord"))
            self.assertLessEqual(len(c), 500)

    def test_every_chunk_within_limit(self):
        text = ("paragraph text here. " * 60 + "\n\n") * 8
        self.assertTrue(all(len(c) <= 900 for c in post.split(text, limit=900)))


class TestPost(Base):
    def test_mention_is_appended_so_the_fiction_leads(self):
        """Pros cue by character name inside the fiction. A beat that opens
        with a raw platform mention leads with machinery instead of world."""
        r = post.post(self.cfg, self.led, "Vesh, the door opens.", cue="vesh",
                      send_fn=self.send)
        self.assertTrue(r["mention_repaired"])
        self.assertTrue(self.sent[0].startswith("Vesh, the door opens."))
        self.assertTrue(self.sent[0].rstrip().endswith("<@42>"))

    def test_split_beat_still_notifies_on_the_first_message(self):
        """A trailing mention on a multi-message beat would notify the seat
        only once the tail lands."""
        long_text = "The chapel is very old. " * 300
        post.post(self.cfg, self.led, long_text, cue="vesh", send_fn=self.send)
        self.assertGreater(len(self.sent), 1)
        self.assertIn("<@42>", self.sent[0])

    def test_existing_mention_is_not_duplicated(self):
        r = post.post(self.cfg, self.led, "<@42> Vesh, go.", cue="vesh",
                      send_fn=self.send)
        self.assertFalse(r["mention_repaired"])
        self.assertEqual(self.sent[0].count("<@42>"), 1)

    def test_human_cue_gets_no_mention(self):
        post.post(self.cfg, self.led, "Rowan, you're up.", cue="rowan",
                  send_fn=self.send)
        self.assertNotIn("<@", self.sent[0])

    def test_post_opens_a_cue_pair(self):
        post.post(self.cfg, self.led, "Rowan?", cue="rowan", send_fn=self.send)
        self.assertEqual(len(pairs.open_now(self.led, "cue")), 1)

    def test_post_records_beat_and_qa(self):
        post.post(self.cfg, self.led, "The tide turns.", send_fn=self.send)
        self.assertEqual(len(self.led.read(etype="ux.beat")), 1)
        self.assertEqual(len(self.led.read(etype="qa.post")), 1)

    def test_failure_is_recorded_and_reported(self):
        def boom(_cfg, _text):
            raise OSError("connection reset")
        r = post.post(self.cfg, self.led, "hello", send_fn=boom)
        self.assertFalse(r["ok"])
        self.assertEqual(len(self.led.read(etype="qa.post_failed")), 1)
        self.assertEqual(len(self.led.read(etype="ux.beat")), 0)

    def test_repair_is_counted_so_the_habit_is_visible(self):
        for _ in range(3):
            post.post(self.cfg, self.led, "Vesh?", cue="vesh", send_fn=self.send)
        repairs = [c for c in self.led.read(etype="qa.command")
                   if c["cmd"] == "mention_repair"]
        self.assertEqual(len(repairs), 3)


class TestIngest(Base):
    def test_records_inbound_without_storing_prose(self):
        ingest.ingest_message(self.cfg, self.led,
                              {"author": "Rowan", "content": "I draw my blade"})
        [rec] = self.led.read(etype="qa.inbound")
        self.assertEqual(rec["seat"], "rowan")
        self.assertNotIn("text", rec)
        self.assertEqual(rec["chars"], len("I draw my blade"))

    def test_keep_text_opts_in(self):
        ingest.ingest_message(self.cfg, self.led,
                              {"author": "Rowan", "content": "I draw"},
                              keep_text=True)
        self.assertEqual(self.led.read(etype="qa.inbound")[0]["text"], "I draw")

    def test_player_words_are_never_read_as_syntax(self):
        ingest.ingest_message(self.cfg, self.led,
                              {"author": "Rowan", "content": "ok !drag"})
        self.assertEqual(self.led.read(lane="uxr"), [])

    def test_gm_echo_is_not_double_counted(self):
        evs = ingest.ingest_message(self.cfg, self.led,
                                    {"id": "gm-1", "author": "the gm bot",
                                     "content": "hi", "is_bot": True})
        self.assertEqual([e["type"] for e in evs], ["qa.inbound", "qa.route"])
        self.assertEqual(self.led.read(etype="qa.route")[0]["reason"], "gm_echo")

    def test_inbound_closes_the_matching_cue(self):
        post.post(self.cfg, self.led, "Rowan?", cue="rowan", send_fn=self.send)
        pid = pairs.open_now(self.led, "cue")[0]["id"]
        ingest.ingest_message(self.cfg, self.led,
                              {"id": "cue-answer", "author": "Rowan",
                               "content": "I answer", "is_bot": False,
                               "pair_id": pid})
        self.assertEqual(pairs.open_now(self.led, "cue"), [])

    def test_inbound_from_another_seat_leaves_the_cue_open(self):
        post.post(self.cfg, self.led, "Rowan?", cue="rowan", send_fn=self.send)
        ingest.ingest_message(self.cfg, self.led,
                              {"id": "other-answer", "author": "Vesh",
                               "content": "I wait", "is_bot": True})
        self.assertEqual(len(pairs.open_now(self.led, "cue")), 1)

    def test_ingest_is_idempotent_when_the_message_has_an_id(self):
        """Polling transports re-read the same window constantly; a GM
        checking the channel twice between beats is normal, and must not
        double a seat's line count."""
        msg = {"id": "m-1", "author": "Rowan", "content": "I go in"}
        for _ in range(4):
            ingest.ingest_message(self.cfg, self.led, msg)
        self.assertEqual(len(self.led.read(etype="qa.inbound")), 1)

    def test_distinct_ids_are_all_recorded(self):
        for i in range(3):
            ingest.ingest_message(self.cfg, self.led,
                                  {"id": f"m-{i}", "author": "Rowan", "content": "hi"})
        self.assertEqual(len(self.led.read(etype="qa.inbound")), 3)

    def test_messages_without_ids_are_not_deduplicated(self):
        """No-ID traffic remains visible but cannot resolve table state."""
        for _ in range(2):
            ingest.ingest_message(self.cfg, self.led,
                                  {"author": "Rowan", "content": "hi"})
        self.assertEqual(len(self.led.read(etype="qa.inbound")), 2)

    def test_idempotent_ingest_does_not_reclose_a_pair(self):
        post.post(self.cfg, self.led, "Rowan?", cue="rowan", send_fn=self.send)
        pid = pairs.open_now(self.led, "cue")[0]["id"]
        msg = {"id": "m-9", "author": "Rowan", "content": "yes",
               "pair_id": pid}
        ingest.ingest_message(self.cfg, self.led, msg)
        ingest.ingest_message(self.cfg, self.led, msg)
        closes = [r for r in self.led.read(etype="out.close")]
        self.assertEqual(len(closes), 1)

    def test_unknown_speaker_stays_unknown_and_is_quarantined(self):
        ingest.ingest_message(self.cfg, self.led,
                              {"id": "guest-1", "author": "Guest",
                               "content": "hi", "is_bot": False})
        self.assertEqual(self.led.read(etype="qa.inbound")[0]["seat"], "unknown")
        [route] = self.led.read(etype="qa.route")
        self.assertEqual((route["status"], route["reason"]),
                         ("quarantined", "unknown_principal"))


#: Embed captured from a live #dnd-table session, 2026-07-31, with the
#: author_id/is_bot envelope fields emitted by the listener. Kept as a fixture
#: because every assumption about this shape can otherwise fail silently.
REAL_BEYOND20 = {
    "id": "real-1", "author": "Beyond 20", "author_id": "relay-20",
    "is_bot": True, "content": "",
    "embeds": [{
        "title": "Initiative (+6)",
        "url": "https://www.dndbeyond.com/characters/93177801",
        "author": {"name": "William Wildmirth P3/W4 Hex/Chain",
                   "url": "https://www.dndbeyond.com/characters/93177801"},
        "footer": {"text": "Rolled using Beyond 20"},
        "fields": [{"name": ":two::zero:",
                    "value": ":game_die: 1d20 + 6 :arrow_right: (14) + 6"}],
    }],
}


class TestRealBeyond20Payload(unittest.TestCase):
    """Regression fixture from a live table. Three things differed from what
    the docs implied, each of which fails silently rather than loudly."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.led = Ledger(os.path.join(self.dir, "s.jsonl"))
        self.cfg = TableConfig({
            "name": "Bell", "data_dir": self.dir,
            "gm": {"id": "gm", "display": "GM"},
            "seats": [{"id": "william", "display": "William", "kind": "human",
                       "sheet_id": "93177801"}],
            "transport": {"roll_relay_bots": ["Beyond 20"]}})

    def test_bot_name_has_a_space_in_it(self):
        """The extension is 'Beyond20'; the bot posts as 'Beyond 20'. Matching
        the product name would attribute nothing, all evening, silently."""
        result = ingest.attribute_relay(self.cfg, REAL_BEYOND20)
        self.assertEqual(result["seat"].id, "william")

    def test_relay_name_matching_ignores_spacing(self):
        cfg = TableConfig({
            "name": "B", "data_dir": self.dir, "gm": {"id": "gm", "display": "GM"},
            "seats": [{"id": "william", "display": "William", "sheet_id": "93177801"}],
            "transport": {"roll_relay_bots": ["Beyond20"]}})
        result = ingest.attribute_relay(cfg, REAL_BEYOND20)
        self.assertEqual(result["seat"].id, "william")

    def test_total_is_decoded_from_keycap_emoji(self):
        """The total exists only as ':two::zero:' in the field NAME."""
        self.assertEqual(ingest.parse_relay_roll(REAL_BEYOND20)["total"], 20)

    def test_spoiler_bars_are_stripped_from_the_breakdown(self):
        msg = json.loads(json.dumps(REAL_BEYOND20))
        msg["embeds"][0]["fields"][0]["value"] = "||:game_die: 1d20 + 6 :arrow_right: (5) + 6||"
        msg["embeds"][0]["fields"][0]["name"] = ":one::one:"
        parsed = ingest.parse_relay_roll(msg)
        self.assertEqual(parsed["total"], 11)
        self.assertNotIn("|", parsed["breakdown"])

    def test_decorated_character_name_is_not_substring_attributed(self):
        """Will must never capture William (or a decorated character name)."""
        cfg = TableConfig({
            "name": "B", "data_dir": self.dir, "gm": {"id": "gm", "display": "GM"},
            "seats": [{"id": "william", "display": "William"}],
            "transport": {"roll_relay_bots": ["Beyond 20"]}})
        result = ingest.attribute_relay(cfg, REAL_BEYOND20)
        self.assertIsNone(result["seat"])
        self.assertEqual(result["reason"], "relay_unattributed")

    def test_sheet_id_wins_over_a_coincidental_name(self):
        cfg = TableConfig({
            "name": "B", "data_dir": self.dir, "gm": {"id": "gm", "display": "GM"},
            "seats": [{"id": "hex", "display": "Hex"},
                      {"id": "william", "display": "William", "sheet_id": "93177801"}],
            "transport": {"roll_relay_bots": ["Beyond 20"]}})
        result = ingest.attribute_relay(cfg, REAL_BEYOND20)
        self.assertEqual(result["seat"].id, "william")

    def test_the_sheets_own_modifier_is_captured(self):
        """Every relayed roll carries the number the character sheet computed,
        for a named check, on a named sheet. Anything deriving modifiers from
        that same sheet can be checked against it for free, on every roll —
        but only if the observation was recorded while it happened."""
        parsed = ingest.parse_relay_roll(REAL_BEYOND20)
        self.assertEqual(parsed["check"], "Initiative")
        self.assertEqual(parsed["modifier"], 6)

    def test_negative_modifiers_are_read_correctly(self):
        msg = json.loads(json.dumps(REAL_BEYOND20))
        msg["embeds"][0]["title"] = "Perception (-1)"
        parsed = ingest.parse_relay_roll(msg)
        self.assertEqual((parsed["check"], parsed["modifier"]), ("Perception", -1))

    def test_a_title_without_a_modifier_still_yields_the_check(self):
        msg = json.loads(json.dumps(REAL_BEYOND20))
        msg["embeds"][0]["title"] = "Hex Blade Attack"
        parsed = ingest.parse_relay_roll(msg)
        self.assertEqual(parsed["check"], "Hex Blade Attack")
        self.assertIsNone(parsed["modifier"])

    def test_observed_modifier_reaches_the_ledger_for_later_comparison(self):
        pairs.open_pair(self.led, "roll", "r1", seat="william", detail="init")
        ingest.ingest_message(self.cfg, self.led, REAL_BEYOND20)
        [act] = self.led.read(etype="act")
        self.assertEqual(act["sheet_modifier"], 6)
        self.assertEqual(act["roll_check"], "Initiative")
        self.assertEqual(act["sheet_id"], "93177801")

    def test_the_roll_lands_in_the_play_ledger(self):
        pairs.open_pair(self.led, "roll", "r1", seat="william", detail="initiative")
        msg = json.loads(json.dumps(REAL_BEYOND20))
        msg["pair_id"] = "r1"
        ingest.ingest_message(self.cfg, self.led, msg)
        [act] = self.led.read(etype="act")
        self.assertEqual(act["roll_total"], 20)
        self.assertEqual(act["actor"], "William")
        self.assertEqual(pairs.open_now(self.led, "roll"), [])


class TestRollRelay(Base):
    """Beyond20-style relays post as themselves, in embeds, on behalf of a
    player. Filed naively, the whole table's dice land under one synthetic
    seat, no roll pair ever closes, and every human reads as silent while
    actually rolling all evening."""

    def setUp(self):
        super().setUp()
        self.cfg = TableConfig(dict(
            CFG, data_dir=self.dir,
            transport={"roll_relay_bots": ["Beyond20"]}))

    def _roll(self, name, total="17"):
        digit = {"0": "zero", "1": "one", "2": "two", "3": "three",
                 "4": "four", "5": "five", "6": "six", "7": "seven",
                 "8": "eight", "9": "nine"}
        keycap = "".join(f":{digit[c]}:" for c in str(total))
        return {"id": f"m-{name}-{total}", "author": "Beyond20",
                "author_id": "relay-1", "is_bot": True, "content": "",
                "embeds": [{"title": f"{name}: Perception",
                            "fields": [{"name": keycap,
                                        "value": "1d20 + 3 -> (14) + 3"}]}]}

    def test_relayed_roll_is_credited_to_the_right_seat(self):
        ingest.ingest_message(self.cfg, self.led, self._roll("Rowan"))
        [rec] = self.led.read(etype="qa.inbound")
        self.assertEqual(rec["seat"], "rowan")
        self.assertEqual(rec["via"], "Beyond20")

    def test_relayed_roll_closes_the_open_roll_pair(self):
        pairs.open_pair(self.led, "roll", "r1", seat="rowan", detail="Perception")
        msg = self._roll("Rowan")
        msg["pair_id"] = "r1"
        ingest.ingest_message(self.cfg, self.led, msg)
        self.assertEqual(pairs.open_now(self.led, "roll"), [])

    def test_unattributable_relay_is_flagged_not_guessed(self):
        """A roll credited to the wrong seat is worse than one credited to
        none."""
        ingest.ingest_message(self.cfg, self.led, self._roll("Somebody Else"))
        [receipt] = self.led.read(etype="qa.inbound")
        self.assertEqual(receipt["seat"], "unknown")
        [route] = self.led.read(etype="qa.route")
        self.assertEqual((route["status"], route["reason"]),
                         ("quarantined", "relay_unattributed"))

    def test_relay_text_is_kept_even_without_keep_text(self):
        """Dice arithmetic is source evidence, not the player's prose."""
        ingest.ingest_message(self.cfg, self.led, self._roll("Rowan"))
        self.assertIn("Perception", self.led.read(etype="qa.inbound")[0]["text"])

    def test_ordinary_bots_are_not_treated_as_relays(self):
        ingest.ingest_message(self.cfg, self.led,
                              {"id": "m-x", "author": "SomeBot",
                               "author_id": "bot-x", "is_bot": True,
                               "content": "hello", "embeds": []})
        self.assertEqual(self.led.read(etype="qa.inbound")[0]["seat"], "unknown")
        self.assertEqual(self.led.read(etype="qa.route")[0]["status"],
                         "quarantined")

    def test_relay_matches_a_seat_alias(self):
        cfg = TableConfig(dict(
            CFG, data_dir=self.dir,
            seats=[{"id": "rowan", "display": "Rowan", "kind": "human",
                    "aliases": ["rowan of the ash"]}],
            transport={"roll_relay_bots": ["Beyond20"]}))
        ingest.ingest_message(cfg, self.led, self._roll("Rowan of the Ash"))
        self.assertEqual(self.led.read(etype="qa.inbound")[0]["seat"], "rowan")


class TestTypedRolls(Base):
    """Not every table has a relay, and the same table will not have one every
    night — somebody joins from a phone, an extension is not installed. A roll
    arriving as ordinary text is a normal case, not a fallback."""

    def _open(self):
        pairs.open_pair(self.led, "roll", "r1", seat="rowan",
                        detail="Perception")

    def test_unambiguous_typed_roll_is_advisory(self):
        self._open()
        ingest.ingest_message(self.cfg, self.led,
                              {"id": "t1", "author": "Rowan", "content": "14"})
        self.assertEqual(len(pairs.open_now(self.led, "roll")), 1)
        self.assertEqual(self.led.read(etype="act"), [])
        [finding] = self.led.read(etype="qc.finding")
        self.assertEqual((finding["check"], finding["candidate_total"]),
                         ("roll_result_advisory", 14))

    def test_natural_twenty_is_understood(self):
        self._open()
        ingest.ingest_message(self.cfg, self.led,
                              {"id": "t2", "author": "Rowan", "content": "nat 20!"})
        self.assertEqual(self.led.read(etype="qc.finding")[0]["candidate_total"], 20)
        self.assertEqual(len(pairs.open_now(self.led, "roll")), 1)

    def test_explicit_sum_uses_the_total_not_the_first_number(self):
        self._open()
        ingest.ingest_message(self.cfg, self.led,
                              {"id": "t3", "author": "Rowan", "content": "18 + 3 = 21"})
        self.assertEqual(self.led.read(etype="qc.finding")[0]["candidate_total"], 21)
        self.assertEqual(len(pairs.open_now(self.led, "roll")), 1)

    def test_ambiguous_number_asks_instead_of_assuming(self):
        """A wrong total silently consumed corrupts the ledger, and nobody
        notices until the arithmetic stops making sense."""
        self._open()
        ingest.ingest_message(
            self.cfg, self.led,
            {"id": "t4", "author": "Rowan",
             "content": "I rolled 18, then got 21 maybe"})
        self.assertEqual(len(pairs.open_now(self.led, "roll")), 1)
        self.assertEqual(self.led.read(etype="act"), [])
        [f] = self.led.read(etype="qc.finding")
        self.assertEqual(f["check"], "roll_result_advisory")
        self.assertEqual(f["severity"], "attention")

    def test_no_detection_when_no_roll_is_outstanding(self):
        """Numbers in ordinary conversation are just conversation."""
        ingest.ingest_message(self.cfg, self.led,
                              {"id": "t5", "author": "Rowan", "content": "14"})
        self.assertEqual(self.led.read(etype="act"), [])
        self.assertEqual(self.led.read(etype="qc.finding"), [])

    def test_prose_with_no_number_is_left_alone(self):
        self._open()
        ingest.ingest_message(self.cfg, self.led,
                              {"id": "t6", "author": "Rowan",
                               "content": "I climb the ladder carefully"})
        self.assertEqual(len(pairs.open_now(self.led, "roll")), 1)
        self.assertEqual(self.led.read(etype="qc.finding"), [])

    def test_report_surfaces_advisory_without_counting_a_roll(self):
        self._open()
        ingest.ingest_message(self.cfg, self.led,
                              {"id": "t7", "author": "Rowan", "content": "14"})
        from tablekit import ux as ux_mod
        stats = ux_mod.transport_stats(self.led)
        self.assertIsNone(stats["rolls_by_route"])
        self.assertEqual(stats["routing_advisories"], 1)


class TestIngestEmits(Base):
    """A recorder that writes to a file and says nothing is half the job."""

    def test_speech_emits_a_readable_line(self):
        msg = {"id": "e1", "author": "Rowan", "content": "I go slow, watching the water"}
        evs = ingest.ingest_message(self.cfg, self.led, msg)
        line = ingest.summarize(self.cfg, self.led, msg, evs)
        self.assertEqual(line, "Rowan: I go slow, watching the water")

    def test_a_relayed_roll_emits_the_total(self):
        cfg = TableConfig(dict(CFG, data_dir=self.dir,
                               transport={"roll_relay_bots": ["Beyond 20"]}))
        msg = {"id": "e2", "author": "Beyond 20", "author_id": "relay-e2",
               "is_bot": True, "content": "",
               "embeds": [{"title": "Perception (+3)",
                           "author": {"name": "Rowan"},
                           "fields": [{"name": ":one::seven:", "value": "1d20 + 3"}]}]}
        evs = ingest.ingest_message(cfg, self.led, msg)
        self.assertEqual(ingest.summarize(cfg, self.led, msg, evs),
                         "Rowan rolled Perception: 17 [Beyond 20]")

    def test_nothing_ingested_emits_nothing(self):
        msg = {"id": "gm-e1", "author": "the gm bot", "is_bot": True,
               "content": "my own beat"}
        evs = ingest.ingest_message(self.cfg, self.led, msg)
        self.assertIsNone(ingest.summarize(self.cfg, self.led, msg, evs))

    def test_long_lines_are_truncated_for_a_notification(self):
        msg = {"id": "e3", "author": "Rowan", "content": "x" * 500}
        evs = ingest.ingest_message(self.cfg, self.led, msg)
        self.assertLessEqual(len(ingest.summarize(self.cfg, self.led, msg, evs)), 250)


class TestEngineTap(Base):
    def test_tap_writes_new_log_lines_once(self):
        state = {"log": ["[round 1] Rowan hits", "[round 1] Goblin falls"],
                 "log_len": 2, "turn": "u1", "units": {"u1": "Rowan [party] 8/10"}}
        engine.tap(self.led, state)
        self.assertEqual(len(self.led.read(etype="act")), 2)
        # Idempotent: same state again writes nothing.
        engine.tap(self.led, state)
        self.assertEqual(len(self.led.read(etype="act")), 2)

    def test_tap_appends_only_the_delta(self):
        engine.tap(self.led, {"log": ["a"], "log_len": 1})
        engine.tap(self.led, {"log": ["a", "b"], "log_len": 2})
        texts = [r["text"] for r in self.led.read(etype="act")]
        self.assertEqual(texts, ["a", "b"])

    def test_rulings_are_events_not_acts(self):
        engine.tap(self.led, {"log": ["[ruling.rowan] advantage granted"],
                              "log_len": 1})
        self.assertEqual(len(self.led.read(etype="event")), 1)

    def test_turn_is_recorded_for_the_active_unit(self):
        engine.tap(self.led, {"log": ["x"], "log_len": 1, "turn": "u1",
                              "units": {"u1": "Rowan [party] 8/10 AC14"}})
        self.assertEqual(self.led.read(etype="turn")[0]["actor"], "Rowan")

    def test_position_survives_in_the_session_file_not_a_sidecar(self):
        engine.tap(self.led, {"log": ["a", "b"], "log_len": 2})
        fresh = Ledger(self.led.path)
        self.assertEqual(engine.synced_through(fresh), 2)


if __name__ == "__main__":
    unittest.main()


class TestMentionFactNotRescan(Base):
    """A verdict must not be re-derived from a lossy copy of evidence.

    Found on a real session: four `undeliverable_cue` defects were reported for
    cues that HAD been delivered. `ux.beat.text` is truncated to 400 chars for
    storage, the mention now trails the beat, and long beats therefore lose it
    to the cut — after which re-scanning the stored copy says it was missing.
    """

    def test_post_records_that_the_mention_was_handled(self):
        post.post(self.cfg, self.led, "Vesh, the door opens.", cue="vesh",
                  send_fn=self.send)
        self.assertTrue(self.led.read(etype="ux.beat")[0]["mention_ok"])

    def test_a_long_delivered_cue_is_not_flagged_after_truncation(self):
        from tablekit import detector
        long_beat = "The chapel is very old and the water keeps rising. " * 12
        post.post(self.cfg, self.led, long_beat, cue="vesh", send_fn=self.send)
        stored = self.led.read(etype="ux.beat")[0]["text"]
        self.assertNotIn("<@42>", stored, "precondition: mention lost to truncation")
        checks = [f["check"] for f in detector.check(self.led, self.cfg)]
        self.assertNotIn("undeliverable_cue", checks)

    def test_a_beat_without_the_flag_is_still_scanned(self):
        """Beats recorded by the CLI carry no flag, so the text scan must still
        protect them."""
        from tablekit import detector
        self.led.append("ux.beat", words=5, chunks=1, cued_seat="vesh",
                        text="Vesh, go.")
        checks = [f["check"] for f in detector.check(self.led, self.cfg)]
        self.assertIn("undeliverable_cue", checks)

    def test_truncated_text_is_never_accused(self):
        """Ambiguity produces silence. A beat stored at the truncation limit is
        known-incomplete evidence — a trailing mention may simply have been cut
        — so it cannot support a finding. This protects sessions recorded
        before mention_ok existed."""
        from tablekit import detector
        self.led.append("ux.beat", words=80, chunks=1, cued_seat="vesh",
                        text="x" * detector._STORED_TEXT_LIMIT)
        checks = [f["check"] for f in detector.check(self.led, self.cfg)]
        self.assertNotIn("undeliverable_cue", checks)

    def test_short_text_without_the_flag_is_still_accused(self):
        from tablekit import detector
        self.led.append("ux.beat", words=5, chunks=1, cued_seat="vesh",
                        text="Vesh, go.")
        checks = [f["check"] for f in detector.check(self.led, self.cfg)]
        self.assertIn("undeliverable_cue", checks)
