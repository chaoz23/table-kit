"""Transport tests — posting, ingestion, and the engine tap.

No network anywhere: `post()` takes a `send_fn` seam and everything else works
on the session file.
"""

import hashlib
import io
import json
import multiprocessing
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import unicodedata
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tablekit import engine, ingest, pairs, post  # noqa: E402
from tablekit.config import ConfigError, TableConfig  # noqa: E402
from tablekit.events import Ledger  # noqa: E402

CFG = {
    "name": "T",
    "gm": {"id": "gm", "display": "GM", "aliases": ["the gm bot"]},
    "seats": [
        {"id": "rowan", "display": "Rowan", "kind": "human"},
        {"id": "vesh", "display": "Vesh", "kind": "agent", "mention": "<@42>"},
    ],
    "transport": {"write_enabled": True},
}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.led = Ledger(os.path.join(self.dir, "s.jsonl"))
        self.cfg = TableConfig(dict(CFG, data_dir=self.dir))
        self.sent = []
        self.payloads = []

    def send(self, _cfg, payload):
        self.payloads.append(payload)
        self.sent.append(payload["content"])
        return {"id": f"msg-{len(self.sent)}", "nonce": payload["nonce"]}


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

    def test_unbroken_input_is_hard_split_inside_the_limit(self):
        text = "x" * 5000
        chunks = post.split(text, limit=2000)
        self.assertEqual("".join(chunks), text)
        self.assertEqual([post.utf16_units(c) for c in chunks], [2000, 2000, 1000])

    def test_astral_characters_use_conservative_utf16_measurement(self):
        text = "😀" * 2500
        chunks = post.split(text, limit=2000)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(post.utf16_units(c) <= 2000 for c in chunks))
        self.assertEqual(len(chunks[0]), 1000)

    def test_zwj_and_combining_graphemes_are_not_split(self):
        family = "👩‍👩‍👧‍👦"
        text = (family + "e\u0301") * 30
        chunks = post.split(text, limit=30)
        self.assertEqual("".join(chunks), text)
        for chunk in chunks:
            self.assertFalse(chunk.startswith("\u200d"))
            self.assertFalse(chunk.endswith("\u200d"))
            self.assertFalse(unicodedata.combining(chunk[0]))

    def test_indic_virama_conjunct_is_not_split(self):
        conjunct = "क्ष"
        chunks = post.split(conjunct * 10, limit=5)
        self.assertEqual("".join(chunks), conjunct * 10)
        self.assertTrue(all(not chunk.startswith("ष") for chunk in chunks))
        self.assertTrue(all(not chunk.endswith("्") for chunk in chunks))

    def test_hangul_jamo_and_prepend_clusters_are_not_split(self):
        hangul = "\u1100\u1161"  # choseong kiyeok + jungseong a
        prepend = "\u0600A"
        zwnj = "a\u200c"
        thai_spacing_mark = "\u0e01\u0e33"
        halfwidth_voiced = "\uff76\uff9e"
        for cluster in (hangul, prepend, zwnj, thai_spacing_mark,
                        halfwidth_voiced):
            with self.subTest(cluster=cluster):
                self.assertEqual(list(post._graphemes(cluster)), [cluster])
        with self.assertRaises(post.PostError):
            post.split(hangul, limit=1)
        with self.assertRaises(post.PostError):
            post.split(prepend, limit=1)

    def test_single_pathological_grapheme_is_refused_not_broken(self):
        with self.assertRaises(post.PostError):
            post.split("a" + "\u0301" * 2100, limit=2000)

    def test_unpaired_surrogate_is_refused(self):
        with self.assertRaises(post.PostError):
            post.split("bad-\ud800", limit=2000)

    def test_one_operation_has_a_hard_message_count_cap(self):
        cfg = TableConfig(dict(CFG, data_dir=tempfile.mkdtemp()))
        led = Ledger(os.path.join(cfg.data_dir, "bounded.jsonl"))
        called = []
        result = post.post(
            cfg, led, "x" * (post.DISCORD_CONTENT_LIMIT
                              * (post.MAX_CHUNKS_PER_OPERATION + 1)),
            send_fn=lambda *_: called.append(True), operation_id="too-large")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("content_too_large", result["error"])
        self.assertEqual(called, [])
        self.assertEqual(led.records(), [])

        result = post.post(
            cfg, led, "x" * post.MAX_SOURCE_UTF16_UNITS, cue="vesh",
            send_fn=lambda *_: called.append(True), operation_id="too-many")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("too_many_chunks", result["error"])
        self.assertEqual(called, [])
        self.assertEqual(led.records(), [])


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

    def test_only_target_user_is_allowed_to_ping_and_only_once(self):
        text = "@everyone <@&99> <@777> <@00042> <@42> Vesh? <@!42>"
        result = post.post(self.cfg, self.led, text, cue="vesh",
                           send_fn=self.send, operation_id="mentions-1")
        self.assertTrue(result["ok"], result)
        rendered = "".join(payload["content"] for payload in self.payloads)
        self.assertEqual(len(re.findall(r"<@!?42>", rendered)), 1)
        self.assertNotIn("<@00042>", rendered)
        self.assertIn("@everyone", rendered)
        self.assertIn("<@&99>", rendered)
        self.assertIn("<@777>", rendered)
        self.assertEqual(self.payloads[0]["allowed_mentions"],
                         {"users": ["42"], "replied_user": False})
        self.assertNotIn("parse", self.payloads[0]["allowed_mentions"])

    def test_target_mention_is_in_first_chunk_without_exceeding_limit(self):
        result = post.post(self.cfg, self.led, "x" * 5000, cue="vesh",
                           send_fn=self.send, operation_id="bounded-mention")
        self.assertTrue(result["ok"])
        self.assertIn("<@42>", self.payloads[0]["content"])
        self.assertEqual(sum(p["content"].count("<@42>") for p in self.payloads), 1)
        self.assertTrue(all(post.utf16_units(p["content"]) <= 2000
                            for p in self.payloads))
        for payload in self.payloads[1:]:
            self.assertEqual(payload["allowed_mentions"],
                             {"parse": [], "replied_user": False})

    def test_posting_is_disabled_without_explicit_config_consent(self):
        cfg = TableConfig(dict(CFG, data_dir=self.dir, transport={}))
        called = []
        result = post.post(cfg, self.led, "hello",
                           send_fn=lambda *_: called.append(True))
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(called, [])
        self.assertEqual(self.led.records(), [])

    def test_write_enabled_must_be_boolean(self):
        with self.assertRaises(ConfigError):
            TableConfig(dict(CFG, data_dir=self.dir,
                             transport={"write_enabled": "yes"}))

    def test_unknown_target_and_malformed_agent_mention_fail_before_send(self):
        result = post.post(self.cfg, self.led, "hello", cue="nobody",
                           send_fn=self.send)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(self.payloads, [])
        malformed = dict(CFG)
        malformed["seats"] = [
            {"id": "vesh", "display": "Vesh", "kind": "agent", "mention": "42"}]
        malformed["transport"] = {"write_enabled": True}
        cfg = TableConfig(dict(malformed, data_dir=self.dir))
        result = post.post(cfg, self.led, "hello", cue="vesh", send_fn=self.send)
        self.assertEqual(result["status"], "invalid")
        self.assertIn("invalid_target_mention", result["error"])
        self.assertEqual(self.payloads, [])

    def test_invalid_kind_fails_before_ledger_or_network(self):
        result = post.post(self.cfg, self.led, "hello", kind={"not": "text"},
                           send_fn=self.send)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(self.payloads, [])
        self.assertEqual(self.led.records(), [])

    def test_live_sender_refuses_missing_recovery_identity_and_token(self):
        no_bot = TableConfig(dict(
            CFG, data_dir=self.dir,
            transport={"write_enabled": True, "channel_id": "123"}))
        result = post.post(no_bot, self.led, "hello")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("missing_bot_user", result["error"])
        self.assertEqual(self.led.records(), [])

        no_token = TableConfig(dict(
            CFG, data_dir=self.dir,
            transport={"write_enabled": True, "channel_id": "123",
                       "bot_user_id": "456", "token_env": "ABSENT_TABLE_TOKEN"}))
        with patch.dict(os.environ, {}, clear=True):
            result = post.post(no_token, self.led, "hello")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("invalid_config", result["error"])
        self.assertEqual(self.led.records(), [])

        bad_id = TableConfig(dict(
            CFG, data_dir=self.dir,
            transport={"write_enabled": True, "channel_id": "not-a-channel",
                       "bot_user_id": "456"}))
        result = post.post(bad_id, self.led, "hello")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("invalid_channel_id", result["error"])
        self.assertEqual(self.led.records(), [])


class TestPostingSaga(Base):
    def test_success_records_prepare_each_receipt_and_commit_last(self):
        result = post.post(self.cfg, self.led, "x" * 3000, cue="vesh",
                           send_fn=self.send, operation_id="saga-success")
        self.assertEqual(result["status"], "committed")
        rows = [r for r in self.led.records()
                if r.get("operation_id") == "saga-success"]
        self.assertEqual(rows[0]["type"], "qa.post.prepare")
        self.assertEqual(rows[-1]["type"], "qa.post.commit")
        self.assertEqual(len([r for r in rows if r["type"] == "qa.post.receipt"]),
                         result["chunks"])
        self.assertEqual(len([r for r in rows if r["type"] == "qa.post"]), 1)
        self.assertEqual(len([r for r in rows if r["type"] == "ux.beat"]), 1)
        self.assertEqual(len([r for r in rows if r["type"] == "out.open"]), 1)
        self.assertEqual(result["message_ids"],
                         [r["message_id"] for r in rows
                          if r["type"] == "qa.post.receipt"])

    def test_completed_operation_retry_is_a_noop(self):
        first = post.post(self.cfg, self.led, "hello", send_fn=self.send,
                          operation_id="same-operation")
        sent = len(self.payloads)
        second = post.post(self.cfg, self.led, "hello", send_fn=self.send,
                           operation_id="same-operation")
        self.assertTrue(first["ok"] and second["ok"])
        self.assertTrue(second["resumed"])
        self.assertEqual(len(self.payloads), sent)
        self.assertEqual(first["message_ids"], second["message_ids"])

    def test_concurrent_same_operation_sends_once_across_threads(self):
        entered = threading.Event()
        release = threading.Event()
        sends = []
        results = []

        def sender(_cfg, payload):
            sends.append(payload["nonce"])
            entered.set()
            self.assertTrue(release.wait(3))
            return {"id": "one-message", "nonce": payload["nonce"]}

        def worker():
            results.append(post.post(
                self.cfg, Ledger(self.led.path), "hello", send_fn=sender,
                operation_id="thread-race"))

        first = threading.Thread(target=worker)
        second = threading.Thread(target=worker)
        first.start()
        self.assertTrue(entered.wait(2))
        second.start()
        release.set()
        first.join(3)
        second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["status"] == "committed"
                            for result in results), results)
        rows = [row for row in Ledger(self.led.path).records()
                if row.get("operation_id") == "thread-race"]
        self.assertEqual(len([row for row in rows
                              if row["type"] == "qa.post.prepare"]), 1)
        self.assertEqual(len([row for row in rows
                              if row["type"] == "qa.post.receipt"]), 1)

    def test_concurrent_different_operations_do_not_block_each_other(self):
        first_entered = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()
        results = []
        first_nonce = post.prepare(
            self.cfg, "first", operation_id="parallel-a")["payloads"][0]["nonce"]

        def sender(_cfg, payload):
            event = first_entered if payload["nonce"] == first_nonce else second_entered
            event.set()
            self.assertTrue(release.wait(3))
            return {"id": payload["nonce"], "nonce": payload["nonce"]}

        def worker(text, operation_id):
            results.append(post.post(
                self.cfg, Ledger(self.led.path), text, send_fn=sender,
                operation_id=operation_id))

        first = threading.Thread(target=worker, args=("first", "parallel-a"))
        second = threading.Thread(target=worker, args=("second", "parallel-b"))
        first.start()
        self.assertTrue(first_entered.wait(2))
        second.start()
        self.assertTrue(second_entered.wait(2))
        release.set()
        first.join(3)
        second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["status"] == "committed"
                            for result in results), results)

    @unittest.skipUnless(
        post.fcntl is not None and "fork" in multiprocessing.get_all_start_methods(),
        "requires POSIX process locks and fork")
    def test_concurrent_same_operation_sends_once_across_processes(self):
        context = multiprocessing.get_context("fork")
        entered = context.Event()
        attempting = context.Event()
        release = context.Event()
        sends = context.Value("i", 0)
        results = context.Queue()
        raw_cfg = dict(CFG, data_dir=self.dir)

        def worker(block):
            cfg = TableConfig(raw_cfg)

            def sender(_cfg, payload):
                with sends.get_lock():
                    sends.value += 1
                if block:
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("test release timed out")
                return {"id": "one-message", "nonce": payload["nonce"]}

            attempting.set()
            results.put(post.post(
                cfg, Ledger(self.led.path), "hello", send_fn=sender,
                operation_id="process-race"))

        first = context.Process(target=worker, args=(True,))
        second = context.Process(target=worker, args=(False,))
        try:
            first.start()
            self.assertTrue(entered.wait(2))
            attempting.clear()
            second.start()
            self.assertTrue(attempting.wait(2))
            time.sleep(0.1)
            self.assertEqual(sends.value, 1)
            release.set()
            first.join(3)
            second.join(3)
            self.assertEqual((first.exitcode, second.exitcode), (0, 0))
            outcomes = [results.get(timeout=2), results.get(timeout=2)]
            self.assertTrue(all(item["status"] == "committed"
                                for item in outcomes), outcomes)
            self.assertEqual(sends.value, 1)
        finally:
            release.set()
            for process in (first, second):
                if process.is_alive():
                    process.terminate()
                process.join(1)
            results.close()
            results.join_thread()

    @unittest.skipUnless(
        post.fcntl is not None and "fork" in multiprocessing.get_all_start_methods(),
        "requires POSIX process locks and fork")
    def test_process_crash_releases_operation_lock(self):
        context = multiprocessing.get_context("fork")
        owned = context.Event()

        def crash_while_owned():
            with post._saga_lock(Ledger(self.led.path), "crash-lock"):
                owned.set()
                os._exit(17)

        process = context.Process(target=crash_while_owned)
        process.start()
        self.assertTrue(owned.wait(2))
        process.join(3)
        self.assertEqual(process.exitcode, 17)
        # No cleanup handler ran in the child; the kernel-released range lock
        # must nevertheless be immediately acquirable by recovery.
        with post._saga_lock(self.led, "crash-lock", timeout_s=0.5):
            pass

    @unittest.skipUnless(post.fcntl is not None, "requires POSIX process locks")
    def test_operation_lock_contention_is_bounded(self):
        owned = threading.Event()
        release = threading.Event()

        def holder():
            with post._saga_lock(self.led, "busy-lock"):
                owned.set()
                release.wait(2)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(owned.wait(1))
        started = time.monotonic()
        try:
            with self.assertRaises(post.PostError) as raised:
                with post._saga_lock(self.led, "busy-lock", timeout_s=0.05):
                    pass
            self.assertEqual(raised.exception.code, "operation_busy")
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            thread.join(2)

    def test_completed_live_retry_is_a_noop_without_a_token(self):
        cfg = TableConfig(dict(
            CFG, data_dir=self.dir,
            transport={"kind": "discord", "write_enabled": True,
                       "channel_id": "123", "bot_user_id": "456",
                       "token_env": "ABSENT_TABLE_TOKEN"}))
        first = post.post(cfg, self.led, "hello", send_fn=self.send,
                          operation_id="live-noop")
        self.assertTrue(first["ok"])
        sent = len(self.payloads)
        with patch.dict(os.environ, {}, clear=True):
            second = post.post(cfg, self.led, "hello",
                               operation_id="live-noop")
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["resumed"])
        self.assertEqual(len(self.payloads), sent)

    def test_destination_is_part_of_the_immutable_plan(self):
        cfg1 = TableConfig(dict(
            CFG, data_dir=self.dir,
            transport={"kind": "discord", "write_enabled": True,
                       "channel_id": "123", "bot_user_id": "456"}))
        cfg2 = TableConfig(dict(
            CFG, data_dir=self.dir,
            transport={"kind": "discord", "write_enabled": True,
                       "channel_id": "999", "bot_user_id": "456"}))
        self.assertNotEqual(
            post.prepare(cfg1, "hello", operation_id="destination-nonce")[
                "payloads"][0]["nonce"],
            post.prepare(cfg2, "hello", operation_id="destination-nonce")[
                "payloads"][0]["nonce"])
        post.post(cfg1, self.led, "hello", send_fn=self.send,
                  operation_id="fixed-destination")
        sent = len(self.payloads)
        result = post.post(cfg2, self.led, "hello", send_fn=self.send,
                           operation_id="fixed-destination")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("operation_mismatch", result["error"])
        self.assertEqual(len(self.payloads), sent)

    def test_operation_id_cannot_be_reused_for_different_content(self):
        post.post(self.cfg, self.led, "first", send_fn=self.send,
                  operation_id="immutable-plan")
        sent = len(self.payloads)
        result = post.post(self.cfg, self.led, "different", send_fn=self.send,
                           operation_id="immutable-plan")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("operation_mismatch", result["error"])
        self.assertEqual(len(self.payloads), sent)

    def test_corrupt_prepare_is_refused_before_network(self):
        plan = post.prepare(self.cfg, "hello", operation_id="corrupt-prepare")
        self.led.append(
            "qa.post.prepare", operation_id="corrupt-prepare",
            plan_digest=plan["plan_digest"], content_digest="sha256:wrong",
            chars=5, chunks=1, source_text="hello",
            payloads=plan["payloads"], transport=plan["transport"])
        result = post.post(self.cfg, self.led, "hello", send_fn=self.send,
                           operation_id="corrupt-prepare")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("operation_mismatch", result["error"])
        self.assertEqual(self.payloads, [])

    def test_malformed_ledger_is_refused_before_network(self):
        # State consumers normally omit diagnostic rows. Posting must not: the
        # unreadable row could be a prepare or receipt for an accepted message.
        with open(self.led.path, "wb") as stream:
            stream.write(b'{"type":"qa.post.receipt","operation_id":"lost"')
        result = post.post(
            self.cfg, self.led, "hello", send_fn=self.send,
            operation_id="malformed-ledger")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("malformed_ledger", result["error"])
        self.assertEqual(self.payloads, [])
        self.assertFalse(any(
            row.get("operation_id") == "malformed-ledger"
            for row in self.led.records()))

    def test_partial_delivery_keeps_intent_and_receipts_but_not_full_beat(self):
        calls = []

        def sender(_cfg, payload):
            calls.append(payload)
            if len(calls) == 1:
                return {"id": "remote-1", "nonce": payload["nonce"]}
            raise urllib.error.HTTPError(
                "https://discord.invalid", 400, "bad request", {},
                io.BytesIO(b'{}'))

        result = post.post(self.cfg, self.led, "x" * 3000, cue="vesh",
                           send_fn=sender, operation_id="partial-1",
                           sleep_fn=lambda _: None, random_fn=lambda: 0)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["chunks_sent"], 1)
        rows = [r for r in self.led.records()
                if r.get("operation_id") == "partial-1"]
        self.assertEqual(len([r for r in rows if r["type"] == "qa.post.prepare"]), 1)
        self.assertEqual(len([r for r in rows if r["type"] == "qa.post.receipt"]), 1)
        self.assertEqual(len([r for r in rows if r["type"] == "qa.post.partial"]), 1)
        self.assertEqual(len([r for r in rows if r["type"] == "qa.post_failed"]), 1)
        self.assertFalse(any(r["type"] == "ux.beat" for r in rows))
        self.assertIn("text", rows[0])
        self.assertEqual(rows[0]["pair_id"],
                         "cue-" + hashlib.sha256(b"partial-1").hexdigest()[:24])
        self.assertEqual(rows[0]["source_text"], "x" * 3000)
        self.assertEqual(rows[0]["payloads"], calls)

    def test_429_uses_server_retry_after_and_same_nonce(self):
        payloads, sleeps = [], []

        def sender(_cfg, payload):
            payloads.append(dict(payload))
            if len(payloads) == 1:
                raise urllib.error.HTTPError(
                    "https://discord.invalid", 429, "rate limited",
                    {"Retry-After": "9"},
                    io.BytesIO(b'{"retry_after":1.25}'))
            return {"id": "remote-ok", "nonce": payload["nonce"]}

        result = post.post(self.cfg, self.led, "hello", send_fn=sender,
                           operation_id="rate-limit", sleep_fn=sleeps.append,
                           random_fn=lambda: 0)
        self.assertTrue(result["ok"])
        self.assertEqual(sleeps, [1.25])
        self.assertEqual(payloads[0]["nonce"], payloads[1]["nonce"])
        self.assertTrue(payloads[0]["enforce_nonce"])

    def test_rate_limit_beyond_budget_refuses_without_early_retry(self):
        attempts, sleeps = [], []

        def sender(_cfg, payload):
            attempts.append(payload["nonce"])
            raise urllib.error.HTTPError(
                "https://discord.invalid", 429, "rate limited", {},
                io.BytesIO(b'{"retry_after":999}'))

        result = post.post(self.cfg, self.led, "hello", send_fn=sender,
                           operation_id="rate-budget", sleep_fn=sleeps.append,
                           random_fn=lambda: 0, retry_budget_s=2)
        self.assertEqual(result["status"], "failed")
        self.assertIn("retry_budget_exceeded", result["error"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(sleeps, [])

    def test_ambiguous_timeout_retries_with_same_enforced_nonce(self):
        payloads, sleeps = [], []

        def sender(_cfg, payload):
            payloads.append(dict(payload))
            if len(payloads) == 1:
                raise TimeoutError("socket timed out")
            return {"id": "same-message", "nonce": payload["nonce"]}

        result = post.post(self.cfg, self.led, "hello", send_fn=sender,
                           operation_id="timeout-retry", sleep_fn=sleeps.append,
                           random_fn=lambda: 0)
        self.assertTrue(result["ok"])
        self.assertEqual(payloads[0]["nonce"], payloads[1]["nonce"])
        self.assertEqual(sleeps, [1.0])

    def test_5xx_retries_with_same_enforced_nonce(self):
        payloads, sleeps = [], []

        def sender(_cfg, payload):
            payloads.append(dict(payload))
            if len(payloads) == 1:
                raise urllib.error.HTTPError(
                    "https://discord.invalid", 503, "unavailable", {},
                    io.BytesIO(b'{}'))
            return {"id": "server-recovered", "nonce": payload["nonce"]}

        result = post.post(self.cfg, self.led, "hello", send_fn=sender,
                           operation_id="server-retry", sleep_fn=sleeps.append,
                           random_fn=lambda: 0)
        self.assertTrue(result["ok"])
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(payloads[0]["nonce"], payloads[1]["nonce"])
        self.assertTrue(payloads[1]["enforce_nonce"])

    def test_retry_budget_is_global_across_all_chunks(self):
        attempts, sleeps = {}, []

        def sender(_cfg, payload):
            nonce = payload["nonce"]
            attempts[nonce] = attempts.get(nonce, 0) + 1
            if attempts[nonce] == 1:
                raise urllib.error.HTTPError(
                    "https://discord.invalid", 429, "rate limited", {},
                    io.BytesIO(b'{"retry_after":1.1}'))
            return {"id": f"message-{len(attempts)}", "nonce": nonce}

        result = post.post(
            self.cfg, self.led, "x" * 3000, send_fn=sender,
            operation_id="global-budget", sleep_fn=sleeps.append,
            random_fn=lambda: 0, retry_budget_s=2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["chunks_sent"], 1)
        self.assertEqual(sleeps, [1.1])

    def test_nonretryable_400_is_not_retried(self):
        attempts = []

        def sender(_cfg, payload):
            attempts.append(payload)
            raise urllib.error.HTTPError(
                "https://discord.invalid", 400, "bad request", {},
                io.BytesIO(b'{}'))

        result = post.post(self.cfg, self.led, "hello", send_fn=sender,
                           operation_id="bad-request", sleep_fn=lambda _: None)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(attempts), 1)

    def test_bare_or_unbound_sender_receipt_is_not_success_or_retried(self):
        attempts = []

        def sender(_cfg, payload):
            attempts.append(payload)
            return "unbound-message-id"

        result = post.post(
            self.cfg, self.led, "hello", send_fn=sender,
            operation_id="unbound-receipt", sleep_fn=lambda _: None)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["delivery_uncertain"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.led.read(etype="qa.post.receipt"), [])
        self.assertEqual(self.led.read(etype="qa.post.commit"), [])

    def test_crash_after_remote_send_reconciles_by_nonce_without_duplicate(self):
        class CrashReceiptLedger(Ledger):
            def __init__(self, path):
                super().__init__(path)
                self.crashed = False

            def append(self, etype, **fields):
                if etype == "qa.post.receipt" and not self.crashed:
                    self.crashed = True
                    raise RuntimeError("crash after remote send")
                return super().append(etype, **fields)

            def append_once(self, etype, unique, **fields):
                if etype == "qa.post.receipt" and not self.crashed:
                    self.crashed = True
                    raise RuntimeError("crash after remote send")
                return super().append_once(etype, unique, **fields)

        crash_ledger = CrashReceiptLedger(self.led.path)
        remote = []

        def first_sender(_cfg, payload):
            remote.append({"id": "remote-once", "nonce": payload["nonce"],
                           "content": payload["content"]})
            return {"id": "remote-once", "nonce": payload["nonce"]}

        with self.assertRaises(RuntimeError):
            post.post(self.cfg, crash_ledger, "hello", send_fn=first_sender,
                      operation_id="receipt-crash")
        second_sends = []
        result = post.post(
            self.cfg, Ledger(self.led.path), "hello",
            send_fn=lambda _cfg, payload: second_sends.append(payload),
            history_fn=lambda _cfg, _since: {"complete": True,
                                             "messages": remote},
            operation_id="receipt-crash")
        self.assertTrue(result["ok"])
        self.assertEqual(second_sends, [])
        self.assertEqual(result["message_ids"], ["remote-once"])
        receipt = Ledger(self.led.path).read(etype="qa.post.receipt")[0]
        self.assertTrue(receipt["reconciled"])

    def test_crash_after_receipts_resumes_finalization_without_resend(self):
        class CrashBeatLedger(Ledger):
            def __init__(self, path):
                super().__init__(path)
                self.crashed = False

            def append(self, etype, **fields):
                if etype == "ux.beat" and not self.crashed:
                    self.crashed = True
                    raise RuntimeError("crash before finalization")
                return super().append(etype, **fields)

            def append_once(self, etype, unique, **fields):
                if etype == "ux.beat" and not self.crashed:
                    self.crashed = True
                    raise RuntimeError("crash before finalization")
                return super().append_once(etype, unique, **fields)

        crash_ledger = CrashBeatLedger(self.led.path)
        with self.assertRaises(RuntimeError):
            post.post(self.cfg, crash_ledger, "hello", send_fn=self.send,
                      operation_id="finalize-crash")
        sent = len(self.payloads)
        result = post.post(self.cfg, Ledger(self.led.path), "hello",
                           send_fn=self.send, operation_id="finalize-crash")
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.payloads), sent)
        rows = [r for r in Ledger(self.led.path).records()
                if r.get("operation_id") == "finalize-crash"]
        self.assertEqual(rows[-1]["type"], "qa.post.commit")

    def test_crash_after_each_durable_saga_step_resumes_once(self):
        steps = [
            "qa.post.prepare", "qa.post.receipt", "ux.beat", "event",
            "qa.command", "out.open", "qa.post", "qa.post.commit",
        ]
        for step in steps:
            with self.subTest(step=step):
                path = os.path.join(self.dir, step.replace(".", "-") + ".jsonl")

                class CrashAfterLedger(Ledger):
                    def __init__(self, ledger_path):
                        super().__init__(ledger_path)
                        self.crashed = False

                    def append(inner_self, etype, **fields):
                        row = super(CrashAfterLedger, inner_self).append(
                            etype, **fields)
                        if etype == step and not inner_self.crashed:
                            inner_self.crashed = True
                            raise RuntimeError(f"crash after {step}")
                        return row

                    def append_once(inner_self, etype, unique, **fields):
                        row = super(CrashAfterLedger, inner_self).append_once(
                            etype, unique, **fields)
                        if etype == step and not inner_self.crashed:
                            inner_self.crashed = True
                            raise RuntimeError(f"crash after {step}")
                        return row

                payloads = []

                def sender(_cfg, payload):
                    payloads.append(dict(payload))
                    return {"id": "only-message", "nonce": payload["nonce"]}

                with self.assertRaises(RuntimeError):
                    post.post(
                        self.cfg, CrashAfterLedger(path), "Vesh?", cue="vesh",
                        send_fn=sender, operation_id=f"crash-{step}")
                result = post.post(
                    self.cfg, Ledger(path), "Vesh?", cue="vesh",
                    send_fn=sender,
                    history_fn=lambda *_: {"complete": True, "messages": []},
                    operation_id=f"crash-{step}")
                self.assertTrue(result["ok"])
                self.assertEqual(len(payloads), 1)
                rows = [row for row in Ledger(path).records()
                        if row.get("operation_id") == f"crash-{step}"]
                self.assertEqual(len([row for row in rows
                                      if row["type"] == step]), 1)
                self.assertEqual(len([row for row in rows
                                      if row["type"] == "qa.post.commit"]), 1)

    def test_corrupt_terminal_or_duplicate_derived_state_is_refused(self):
        class CrashAfterType(Ledger):
            def __init__(self, path, target):
                super().__init__(path)
                self.target = target
                self.crashed = False

            def append(self, etype, **fields):
                row = super().append(etype, **fields)
                if etype == self.target and not self.crashed:
                    self.crashed = True
                    raise RuntimeError(f"crash after {etype}")
                return row

            def append_once(self, etype, unique, **fields):
                row = super().append_once(etype, unique, **fields)
                if etype == self.target and not self.crashed:
                    self.crashed = True
                    raise RuntimeError(f"crash after {etype}")
                return row

        commit_path = os.path.join(self.dir, "bad-commit.jsonl")
        with self.assertRaises(RuntimeError):
            post.post(
                self.cfg, CrashAfterType(commit_path, "qa.post"), "hello",
                send_fn=self.send, operation_id="bad-commit")
        Ledger(commit_path).append(
            "qa.post.commit", operation_id="bad-commit", chunks=1,
            message_ids=["not-the-receipted-message"])
        sent = len(self.payloads)
        result = post.post(
            self.cfg, Ledger(commit_path), "hello", send_fn=self.send,
            operation_id="bad-commit")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("commit record disagrees", result["error"])
        self.assertEqual(len(self.payloads), sent)

        pair_path = os.path.join(self.dir, "duplicate-pair.jsonl")
        with self.assertRaises(RuntimeError):
            post.post(
                self.cfg, CrashAfterType(pair_path, "out.open"), "Vesh?",
                cue="vesh", send_fn=self.send, operation_id="duplicate-pair")
        Ledger(pair_path).append(
            "out.open", operation_id="duplicate-pair", pair="cue",
            id="cue-wrong", seat="vesh", detail="wrong", beat=1)
        sent = len(self.payloads)
        result = post.post(
            self.cfg, Ledger(pair_path), "Vesh?", cue="vesh",
            send_fn=self.send, operation_id="duplicate-pair")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("duplicate out.open", result["error"])
        self.assertEqual(len(self.payloads), sent)

    def test_commit_without_all_preceding_final_state_is_not_success(self):
        plan = post.prepare(self.cfg, "hello", operation_id="bare-commit")
        self.led.append(
            "qa.post.prepare", operation_id="bare-commit",
            plan_digest=plan["plan_digest"],
            content_digest=plan["content_digest"], chars=5, chunks=1,
            source_text="hello", text="hello", payloads=plan["payloads"],
            transport=plan["transport"])
        self.led.append(
            "qa.post.receipt", operation_id="bare-commit", chunk_index=0,
            message_id="remote-1", nonce=plan["payloads"][0]["nonce"])
        self.led.append(
            "qa.post.commit", operation_id="bare-commit", chunks=1,
            message_ids=["remote-1"])
        called = []
        result = post.post(
            self.cfg, self.led, "hello",
            send_fn=lambda *_: called.append(True), operation_id="bare-commit")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("requires exactly one ux.beat", result["error"])
        self.assertEqual(called, [])

    def test_partial_operation_reconciles_coverage_then_sends_only_missing(self):
        first_calls = []

        def first(_cfg, payload):
            first_calls.append(payload)
            if len(first_calls) == 1:
                return {"id": "first", "nonce": payload["nonce"]}
            raise urllib.error.HTTPError(
                "https://discord.invalid", 400, "bad", {}, io.BytesIO(b'{}'))

        partial = post.post(self.cfg, self.led, "x" * 3000, send_fn=first,
                            operation_id="partial-resume")
        self.assertEqual(partial["status"], "partial")
        resumed_payloads = []

        def second(_cfg, payload):
            resumed_payloads.append(payload)
            return {"id": "second", "nonce": payload["nonce"]}

        result = post.post(
            self.cfg, self.led, "x" * 3000, send_fn=second,
            history_fn=lambda *_: {"complete": True, "messages": []},
            operation_id="partial-resume")
        self.assertTrue(result["ok"])
        self.assertEqual(len(resumed_payloads), 1)
        self.assertEqual(result["message_ids"], ["first", "second"])

    def test_partial_operation_can_resume_from_ledger_plan_alone(self):
        calls = []

        def first(_cfg, payload):
            calls.append(payload)
            if len(calls) == 1:
                return {"id": "first", "nonce": payload["nonce"]}
            raise urllib.error.HTTPError(
                "https://discord.invalid", 400, "bad", {}, io.BytesIO(b'{}'))

        partial = post.post(
            self.cfg, self.led, "Vesh, " + "x" * 3000, cue="vesh",
            send_fn=first, operation_id="ledger-resume")
        self.assertEqual(partial["status"], "partial")
        resumed_payloads = []

        def second(_cfg, payload):
            resumed_payloads.append(payload)
            return {"id": f"second-{len(resumed_payloads)}",
                    "nonce": payload["nonce"]}

        result = post.resume(
            self.cfg, self.led, "ledger-resume", send_fn=second,
            history_fn=lambda *_: {"complete": True, "messages": []})
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(resumed_payloads), 2)
        self.assertEqual(result["message_ids"],
                         ["first", "second-1", "second-2"])

    def test_resume_refuses_without_exactly_one_durable_plan(self):
        result = post.resume(self.cfg, self.led, "not-prepared",
                             send_fn=self.send)
        self.assertEqual(result["status"], "invalid")
        self.assertIn("recovery_plan_unavailable", result["error"])
        self.assertEqual(self.payloads, [])

    def test_incomplete_history_refuses_unreceipted_retry(self):
        class CrashReceiptLedger(Ledger):
            def append(self, etype, **fields):
                if etype == "qa.post.receipt":
                    raise RuntimeError("receipt crash")
                return super().append(etype, **fields)

            def append_once(self, etype, unique, **fields):
                if etype == "qa.post.receipt":
                    raise RuntimeError("receipt crash")
                return super().append_once(etype, unique, **fields)

        with self.assertRaises(RuntimeError):
            post.post(self.cfg, CrashReceiptLedger(self.led.path), "hello",
                      send_fn=self.send, operation_id="history-gap")
        sent = len(self.payloads)
        result = post.post(
            self.cfg, Ledger(self.led.path), "hello", send_fn=self.send,
            history_fn=lambda *_: {"complete": False, "messages": []},
            operation_id="history-gap")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("reconciliation_incomplete", result["error"])
        self.assertEqual(len(self.payloads), sent)

    def test_duplicate_remote_nonce_is_ambiguous_and_refused(self):
        class CrashReceiptLedger(Ledger):
            def append(self, etype, **fields):
                if etype == "qa.post.receipt":
                    raise RuntimeError("receipt crash")
                return super().append(etype, **fields)

            def append_once(self, etype, unique, **fields):
                if etype == "qa.post.receipt":
                    raise RuntimeError("receipt crash")
                return super().append_once(etype, unique, **fields)

        captured = []

        def sender(_cfg, payload):
            captured.append(payload)
            return {"id": "r1", "nonce": payload["nonce"]}

        with self.assertRaises(RuntimeError):
            post.post(self.cfg, CrashReceiptLedger(self.led.path), "hello",
                      send_fn=sender, operation_id="duplicate-history")
        nonce, content = captured[0]["nonce"], captured[0]["content"]
        history = {"complete": True, "messages": [
            {"id": "r1", "nonce": nonce, "content": content},
            {"id": "r2", "nonce": nonce, "content": content},
        ]}
        result = post.post(
            self.cfg, Ledger(self.led.path), "hello", send_fn=self.send,
            history_fn=lambda *_: history, operation_id="duplicate-history")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("ambiguous_history", result["error"])

    def test_reconciliation_authenticates_author_and_content(self):
        cfg = TableConfig(dict(
            CFG, data_dir=self.dir,
            transport={"write_enabled": True, "bot_user_id": "999"}))

        class CrashReceiptLedger(Ledger):
            def append(self, etype, **fields):
                if etype == "qa.post.receipt":
                    raise RuntimeError("receipt crash")
                return super().append(etype, **fields)

            def append_once(self, etype, unique, **fields):
                if etype == "qa.post.receipt":
                    raise RuntimeError("receipt crash")
                return super().append_once(etype, unique, **fields)

        remote = []

        def first(_cfg, payload):
            remote.append(dict(payload))
            return {"id": "remote", "nonce": payload["nonce"]}

        with self.assertRaises(RuntimeError):
            post.post(cfg, CrashReceiptLedger(self.led.path), "hello",
                      send_fn=first, operation_id="authenticated-history")
        payload = remote[0]
        second_sends = []
        wrong_author = {
            "id": "spoof", "nonce": payload["nonce"],
            "content": payload["content"], "author": {"id": "123"},
        }
        result = post.post(
            cfg, Ledger(self.led.path), "hello",
            send_fn=lambda _cfg, item: second_sends.append(item) or {
                "id": "actual", "nonce": item["nonce"]},
            history_fn=lambda *_: {"complete": True,
                                   "messages": [wrong_author]},
            operation_id="authenticated-history")
        self.assertTrue(result["ok"])
        self.assertEqual(len(second_sends), 1)

        mismatch_path = os.path.join(self.dir, "content-mismatch.jsonl")
        remote.clear()
        with self.assertRaises(RuntimeError):
            post.post(cfg, CrashReceiptLedger(mismatch_path), "hello",
                      send_fn=first, operation_id="content-history")
        payload = remote[0]
        mismatch = {
            "id": "remote", "nonce": payload["nonce"], "content": "changed",
            "author": {"id": "999"},
        }
        result = post.post(
            cfg, Ledger(mismatch_path), "hello", send_fn=self.send,
            history_fn=lambda *_: {"complete": True, "messages": [mismatch]},
            operation_id="content-history")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("reconcile_content_mismatch", result["error"])

    def test_builtin_history_pages_until_prepare_time_is_covered(self):
        cfg = TableConfig(dict(
            CFG, data_dir=self.dir,
            transport={"write_enabled": True, "channel_id": "123",
                       "bot_user_id": "999", "token_env": "TEST_TABLE_TOKEN"}))
        pages = [
            [
                {"id": "1533052169748480000",
                 "timestamp": "2026-08-01T10:02:00Z"},
                {"id": "1533051918090240000",
                 "timestamp": "2026-08-01T10:01:00Z"},
            ],
            [{"id": "1533051414773760000",
              "timestamp": "2026-08-01T09:59:00Z"}],
        ]
        requests = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        def fake_urlopen(request, timeout):
            requests.append((request, timeout))
            return Response(json.dumps(pages.pop(0)).encode("utf-8"))

        since = post._parse_timestamp("2026-08-01T10:00:00Z")
        with patch.dict(os.environ, {"TEST_TABLE_TOKEN": "not-a-real-token"}), \
                patch("tablekit.post.urllib.request.urlopen", fake_urlopen):
            result = post.discord_history_since(cfg, since)
        self.assertTrue(result["complete"])
        self.assertEqual([item["id"] for item in result["messages"]],
                         ["1533052169748480000", "1533051918090240000",
                          "1533051414773760000"])
        self.assertEqual(len(requests), 2)
        self.assertNotIn("before=", requests[0][0].full_url)
        self.assertIn("before=1533051918090240000", requests[1][0].full_url)
        self.assertEqual(requests[0][1], 20)

    def test_builtin_history_rejects_malformed_coverage_evidence(self):
        cfg = TableConfig(dict(
            CFG, data_dir=self.dir,
            transport={"write_enabled": True, "channel_id": "123",
                       "bot_user_id": "999", "token_env": "TEST_TABLE_TOKEN"}))
        # An old timestamp injected ahead of a newer message used to make the
        # page look as though it covered the prepare boundary and permit resend.
        malformed = [
            {"id": "1533052169748480000",
             "timestamp": "2026-08-01T09:59:00Z"},
            {"id": "1533051918090240000",
             "timestamp": "2026-08-01T10:02:00Z"},
        ]

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        def fake_urlopen(_request, timeout):
            del timeout
            return Response(json.dumps(malformed).encode("utf-8"))

        since = post._parse_timestamp("2026-08-01T10:00:00Z")
        with patch.dict(os.environ, {"TEST_TABLE_TOKEN": "not-a-real-token"}), \
                patch("tablekit.post.urllib.request.urlopen", fake_urlopen), \
                self.assertRaises(post.PostError) as raised:
            post.discord_history_since(cfg, since)
        self.assertEqual(raised.exception.code, "invalid_history")

    def test_discord_send_serializes_safety_fields(self):
        cfg = TableConfig(dict(
            CFG, data_dir=self.dir,
            transport={"write_enabled": True, "channel_id": "123",
                       "bot_user_id": "999",
                       "token_env": "TEST_TABLE_TOKEN"}))
        payload = post.prepare(cfg, "@everyone hi", operation_id="wire-body")[
            "payloads"][0]
        captured = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        def fake_urlopen(request, timeout):
            captured.append((request, timeout))
            return Response(json.dumps({
                "id": "123456789", "nonce": payload["nonce"],
                "content": payload["content"], "channel_id": "123",
                "author": {"id": "999"},
            }).encode())

        with patch.dict(os.environ, {"TEST_TABLE_TOKEN": "not-a-real-token"}), \
                patch("tablekit.post.urllib.request.urlopen", fake_urlopen):
            result = post.discord_send(cfg, payload)
        self.assertEqual(result["id"], "123456789")
        body = json.loads(captured[0][0].data)
        self.assertEqual(body, payload)
        self.assertEqual(body["allowed_mentions"],
                         {"parse": [], "replied_user": False})
        self.assertTrue(body["enforce_nonce"])

    def test_discord_success_must_bind_id_nonce_content_author_and_channel(self):
        cfg = TableConfig(dict(
            CFG, data_dir=self.dir,
            transport={"write_enabled": True, "channel_id": "123",
                       "bot_user_id": "999",
                       "token_env": "TEST_TABLE_TOKEN"}))
        payload = post.prepare(cfg, "hello", operation_id="bound-response")[
            "payloads"][0]

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        response = {
            "id": "123456789", "nonce": payload["nonce"],
            "content": payload["content"], "channel_id": "123",
            "author": {"id": "not-the-configured-bot"},
        }

        def fake_urlopen(_request, timeout):
            del timeout
            return Response(json.dumps(response).encode())

        with patch.dict(os.environ, {"TEST_TABLE_TOKEN": "not-a-real-token"}), \
                patch("tablekit.post.urllib.request.urlopen", fake_urlopen), \
                self.assertRaises(post.DeliveryError) as raised:
            post.discord_send(cfg, payload)
        self.assertEqual(raised.exception.code, "author_mismatch")
        self.assertFalse(raised.exception.retryable)
        self.assertTrue(raised.exception.delivery_uncertain)


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
    def assert_sync_error(self, code, state, ledger=None):
        with self.assertRaises(engine.EngineSyncError) as caught:
            engine.tap(ledger or self.led, state)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_tap_writes_new_log_lines_once(self):
        state = {"log": ["[round 1] Rowan hits", "[round 1] Goblin falls"],
                 "log_len": 2, "turn": "u1", "units": {"u1": "Rowan [party] 8/10"}}
        written = engine.tap(self.led, state)
        self.assertEqual(len(self.led.read(etype="act")), 2)
        self.assertEqual([r["type"] for r in written], ["act", "act", "turn"])
        self.assertEqual(len(self.led.read(etype="qc.mark")), 1)
        # Idempotent: same state again writes nothing.
        self.assertEqual(engine.tap(self.led, state), [])
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

    def test_ten_entry_source_with_only_four_tail_entries_refuses_without_advancing(self):
        state = {"log_tail": ["g", "h", "i", "j"], "log_len": 10}
        err = self.assert_sync_error("source_gap", state)
        self.assertEqual(err.context["first_missing"], 1)
        self.assertEqual(engine.synced_through(self.led), 0)
        self.assertEqual(self.led.read(etype="act"), [])
        self.assertEqual(self.led.read(etype="qc.mark"), [])

    def test_tail_must_begin_at_the_next_expected_entry(self):
        engine.tap(self.led, {"log": ["a", "b", "c", "d"], "log_len": 4})
        self.assert_sync_error(
            "source_gap",
            {"log_tail": ["g", "h", "i", "j"], "log_len": 10},
        )
        self.assertEqual(engine.synced_through(self.led), 4)
        engine.tap(self.led, {"log": list("abcdefghij"), "log_len": 10})
        self.assertEqual(engine.synced_through(self.led), 10)
        self.assertEqual([r["text"] for r in self.led.read(etype="act")],
                         list("abcdefghij"))

    def test_contiguous_tail_can_resume_after_restart(self):
        engine.tap(self.led, {"log": ["a", "b"], "log_len": 2})
        fresh = Ledger(self.led.path)
        written = engine.tap(fresh, {"log_tail": ["c", "d"], "log_len": 4})
        self.assertEqual([r["engine_log_index"] for r in written
                          if "engine_log_index" in r], [3, 4])
        self.assertEqual(engine.synced_through(fresh), 4)
        self.assertEqual(engine.tap(fresh, {"log_tail": ["c", "d"],
                                            "log_len": 4}), [])

    def test_paginated_catch_up_advances_only_to_each_committed_page(self):
        engine.tap(self.led, {"log_tail": ["a", "b"], "log_len": 2})
        self.assertEqual(engine.synced_through(self.led), 2)
        engine.tap(self.led, {"log_tail": ["c", "d"], "log_len": 4})
        self.assertEqual(engine.synced_through(self.led), 4)
        engine.tap(self.led, {"log_tail": ["e"], "log_len": 5})
        self.assertEqual(engine.synced_through(self.led), 5)
        self.assertEqual([r["text"] for r in self.led.read(etype="act")],
                         list("abcde"))

    def test_reordered_history_is_typed_and_does_not_advance(self):
        engine.tap(self.led, {"log": ["a", "b", "c"], "log_len": 3})
        self.assert_sync_error(
            "source_reordered", {"log": ["a", "c", "b"], "log_len": 3})
        self.assertEqual(engine.synced_through(self.led), 3)

    def test_forked_history_is_typed_and_does_not_advance(self):
        engine.tap(self.led, {"log": ["a", "b"], "log_len": 2})
        self.assert_sync_error("source_fork", {"log": ["a", "x"], "log_len": 2})
        self.assertEqual(engine.synced_through(self.led), 2)

    def test_source_id_change_is_a_fork_even_when_text_matches(self):
        state = {"log": ["a"], "log_len": 1, "source_id": "match-a"}
        engine.tap(self.led, state)
        self.assert_sync_error(
            "source_fork",
            {"log": ["a"], "log_len": 1, "source_id": "match-b"},
        )

    def test_reset_and_cursor_ahead_are_distinct_typed_refusals(self):
        engine.tap(self.led, {"log": ["a", "b"], "log_len": 2})
        self.assert_sync_error("source_reset", {"log": [], "log_len": 0})
        self.assert_sync_error("cursor_ahead", {"log": ["a"], "log_len": 1})
        self.assertEqual(engine.synced_through(self.led), 2)

    def test_empty_tail_cannot_claim_a_duplicate_cursor(self):
        engine.tap(self.led, {"log": ["a"], "log_len": 1})
        self.assert_sync_error("source_unverifiable", {"log_tail": [], "log_len": 1})

    def test_durable_entry_recovers_after_cursor_mark_write_fails(self):
        class FailFirstMark(Ledger):
            failed = False

            def append(inner_self, etype, **fields):
                if etype == "qc.mark" and not inner_self.failed:
                    inner_self.failed = True
                    raise OSError("simulated mark write failure")
                return super(FailFirstMark, inner_self).append(etype, **fields)

        failing = FailFirstMark(self.led.path)
        with self.assertRaises(OSError):
            engine.tap(failing, {"log": ["a"], "log_len": 1})
        fresh = Ledger(self.led.path)
        self.assertEqual(engine.synced_through(fresh), 1)
        self.assertEqual(len(fresh.read(etype="act")), 1)
        recovered = engine.tap(fresh, {"log": ["a"], "log_len": 1})
        self.assertEqual(recovered, [])
        self.assertEqual(len(fresh.read(etype="act")), 1)
        self.assertEqual(len(fresh.read(etype="qc.mark")), 1)

    def test_failure_before_first_event_does_not_advance_cursor(self):
        class FailFirstEvent(Ledger):
            failed = False

            def append(inner_self, etype, **fields):
                if etype in ("act", "event") and not inner_self.failed:
                    inner_self.failed = True
                    raise OSError("simulated event write failure")
                return super(FailFirstEvent, inner_self).append(etype, **fields)

        failing = FailFirstEvent(self.led.path)
        with self.assertRaises(OSError):
            engine.tap(failing, {"log": ["a", "b"], "log_len": 2})
        fresh = Ledger(self.led.path)
        self.assertEqual(engine.synced_through(fresh), 0)
        engine.tap(fresh, {"log": ["a", "b"], "log_len": 2})
        self.assertEqual(engine.synced_through(fresh), 2)

    def test_legacy_length_only_cursor_refuses_instead_of_guessing(self):
        self.led.append("qc.mark", narrated_through=0, engine_log_len=10)
        self.assert_sync_error(
            "legacy_cursor_unverifiable",
            {"log_tail": ["g", "h", "i", "j"], "log_len": 10},
        )

    def test_narration_advances_only_with_a_correlated_acknowledgment(self):
        from tablekit import detector

        written = engine.tap(self.led, {"log": ["a", "b"], "log_len": 2})
        events = [r for r in written if "engine_log_index" in r]
        self.assertEqual(engine.narrated_through(self.led), 0)
        # A scalar supplied by the source is not narration evidence.
        found = [f for f in detector.check(
            self.led, self.cfg, state={"log_len": 2, "narrated_through": 2})
            if f["check"] == "unnarrated"]
        self.assertIn("2 engine event(s)", found[0]["detail"])

        ack = engine.acknowledge_narration(self.led, events[0], evidence="discord:m1")
        self.assertEqual(ack[0]["ack_engine_log_index"], 1)
        self.assertEqual(engine.narrated_through(self.led), 1)
        self.assertEqual(engine.acknowledge_narration(self.led, events[0]), [])
        found = [f for f in detector.check(self.led, self.cfg, state={"log_len": 2})
                 if f["check"] == "unnarrated"]
        self.assertIn("1 engine event(s)", found[0]["detail"])

        engine.acknowledge_narration(self.led, events[1])
        self.assertEqual(engine.narrated_through(Ledger(self.led.path)), 2)
        self.assertEqual([f for f in detector.check(
            self.led, self.cfg, state={"log_len": 2})
            if f["check"] == "unnarrated"], [])

    def test_out_of_order_narration_ack_does_not_cover_an_unacknowledged_gap(self):
        written = engine.tap(self.led, {"log": ["a", "b"], "log_len": 2})
        events = [r for r in written if "engine_log_index" in r]
        ack = engine.acknowledge_narration(self.led, events[1])
        self.assertEqual(ack[0]["narrated_through"], 0)
        self.assertEqual(engine.narrated_through(self.led), 0)
        ack = engine.acknowledge_narration(self.led, events[0])
        self.assertEqual(ack[0]["narrated_through"], 2)
        self.assertEqual(engine.narrated_through(self.led), 2)

    def test_source_identity_can_be_adopted_at_an_existing_cursor(self):
        engine.tap(self.led, {"log": ["a"], "log_len": 1, "turn": "u1",
                              "units": {"u1": "Rowan [party] 8/10"}})
        self.assertEqual(engine.tap(
            self.led, {"log": ["a"], "log_len": 1, "source_id": "match-a",
                       "turn": "u1", "units": {"u1": "Rowan [party] 8/10"}}), [])
        self.assertEqual(len(self.led.read(etype="turn")), 1)
        self.assert_sync_error(
            "source_unverifiable", {"log": ["a"], "log_len": 1})
        self.assert_sync_error(
            "source_fork",
            {"log": ["a"], "log_len": 1, "source_id": "match-b"},
        )

    def test_mismatched_narration_acknowledgment_cannot_advance(self):
        written = engine.tap(self.led, {"log": ["a"], "log_len": 1})
        event = next(r for r in written if "engine_log_index" in r)
        forged = dict(event, engine_log_fingerprint="0" * 64)
        with self.assertRaises(engine.EngineSyncError) as caught:
            engine.acknowledge_narration(self.led, forged)
        self.assertEqual(caught.exception.code, "narration_ack_mismatch")
        self.assertEqual(engine.narrated_through(self.led), 0)

    def test_narration_ack_rejects_non_integer_index(self):
        written = engine.tap(self.led, {"log": ["a"], "log_len": 1})
        event = next(r for r in written if "engine_log_index" in r)
        with self.assertRaises(engine.EngineSyncError) as caught:
            engine.acknowledge_narration(
                self.led, dict(event, engine_log_index=True))
        self.assertEqual(caught.exception.code, "narration_ack_invalid")


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
