"""Transport tests — posting, ingestion, and the engine tap.

No network anywhere: `post()` takes a `send_fn` seam and everything else works
on the session file.
"""

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
                                    {"author": "the gm bot", "content": "hi"})
        self.assertEqual(evs, [])
        self.assertEqual(self.led.read(etype="qa.inbound"), [])

    def test_inbound_closes_the_matching_cue(self):
        post.post(self.cfg, self.led, "Rowan?", cue="rowan", send_fn=self.send)
        ingest.ingest_message(self.cfg, self.led,
                              {"author": "Rowan", "content": "I answer"})
        self.assertEqual(pairs.open_now(self.led, "cue"), [])

    def test_inbound_from_another_seat_leaves_the_cue_open(self):
        post.post(self.cfg, self.led, "Rowan?", cue="rowan", send_fn=self.send)
        ingest.ingest_message(self.cfg, self.led,
                              {"author": "Vesh", "content": "I wait"})
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
        """A transport that cannot supply ids gets at-least-once, not silence."""
        for _ in range(2):
            ingest.ingest_message(self.cfg, self.led,
                                  {"author": "Rowan", "content": "hi"})
        self.assertEqual(len(self.led.read(etype="qa.inbound")), 2)

    def test_idempotent_ingest_does_not_reclose_a_pair(self):
        post.post(self.cfg, self.led, "Rowan?", cue="rowan", send_fn=self.send)
        msg = {"id": "m-9", "author": "Rowan", "content": "yes"}
        ingest.ingest_message(self.cfg, self.led, msg)
        ingest.ingest_message(self.cfg, self.led, msg)
        closes = [r for r in self.led.read(etype="out.close")]
        self.assertEqual(len(closes), 1)

    def test_unknown_speaker_is_kept_under_their_own_name(self):
        ingest.ingest_message(self.cfg, self.led,
                              {"author": "Guest", "content": "hi"})
        self.assertEqual(self.led.read(etype="qa.inbound")[0]["seat"], "Guest")


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
        return {"id": f"m-{name}-{total}", "author": "Beyond20", "content": "",
                "embeds": [{"title": f"{name}: Perception",
                            "description": f"Result: {total}"}]}

    def test_relayed_roll_is_credited_to_the_right_seat(self):
        ingest.ingest_message(self.cfg, self.led, self._roll("Rowan"))
        [rec] = self.led.read(etype="qa.inbound")
        self.assertEqual(rec["seat"], "rowan")
        self.assertEqual(rec["via"], "Beyond20")

    def test_relayed_roll_closes_the_open_roll_pair(self):
        pairs.open_pair(self.led, "roll", "r1", seat="rowan", detail="Perception")
        ingest.ingest_message(self.cfg, self.led, self._roll("Rowan"))
        self.assertEqual(pairs.open_now(self.led, "roll"), [])

    def test_unattributable_relay_is_flagged_not_guessed(self):
        """A roll credited to the wrong seat is worse than one credited to
        none."""
        ingest.ingest_message(self.cfg, self.led, self._roll("Somebody Else"))
        self.assertEqual(self.led.read(etype="qa.inbound"), [])
        [c] = [r for r in self.led.read(etype="qa.command")
               if r["cmd"] == "relay_unattributed"]
        self.assertFalse(c["ok"])

    def test_relay_text_is_kept_even_without_keep_text(self):
        """Dice arithmetic is not the player's prose, and it is the evidence
        that closed the pair."""
        ingest.ingest_message(self.cfg, self.led, self._roll("Rowan"))
        self.assertIn("Perception", self.led.read(etype="qa.inbound")[0]["text"])

    def test_ordinary_bots_are_not_treated_as_relays(self):
        ingest.ingest_message(self.cfg, self.led,
                              {"id": "m-x", "author": "SomeBot",
                               "content": "hello", "embeds": []})
        self.assertEqual(self.led.read(etype="qa.inbound")[0]["seat"], "SomeBot")

    def test_relay_matches_a_seat_alias(self):
        cfg = TableConfig(dict(
            CFG, data_dir=self.dir,
            seats=[{"id": "rowan", "display": "Rowan", "kind": "human",
                    "aliases": ["rowan of the ash"]}],
            transport={"roll_relay_bots": ["Beyond20"]}))
        ingest.ingest_message(cfg, self.led, self._roll("Rowan of the Ash"))
        self.assertEqual(self.led.read(etype="qa.inbound")[0]["seat"], "rowan")


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
