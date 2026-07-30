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
    def test_mention_is_prepended_for_agent_cue(self):
        r = post.post(self.cfg, self.led, "Vesh, the door opens.", cue="vesh",
                      send_fn=self.send)
        self.assertTrue(r["mention_repaired"])
        self.assertTrue(self.sent[0].startswith("<@42>"))

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

    def test_markers_extracted_from_inbound(self):
        ingest.ingest_message(self.cfg, self.led,
                              {"author": "Rowan", "content": "ok !drag"})
        self.assertEqual(self.led.read(etype="uxr.marker")[0]["marker"], "drag")

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

    def test_unknown_speaker_is_kept_under_their_own_name(self):
        ingest.ingest_message(self.cfg, self.led,
                              {"author": "Guest", "content": "hi"})
        self.assertEqual(self.led.read(etype="qa.inbound")[0]["seat"], "Guest")


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
