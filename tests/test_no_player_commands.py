"""The product non-goal, made checkable.

**Players are never given a command syntax.** Not tokens, not aliases, not
keywords, not an "advanced mode". The reason is in `tablekit.uxr`: text RPGs
gave players verb lists because 1980s parsers could not understand a sentence,
and that workaround is precisely what makes them feel like operating a computer
instead of sitting with someone running a world. An agent GM that reintroduces
it has thrown away the only thing it has over a parser game.

A stated principle drifts. These tests are the reason it cannot: any future
change that starts reading player prose as syntax, or that advertises a token
to players, fails here.
"""

import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tablekit import cli, ingest, uxr  # noqa: E402
from tablekit.config import TableConfig  # noqa: E402
from tablekit.events import Ledger  # noqa: E402

CFG = {
    "name": "T",
    "gm": {"id": "gm", "display": "GM"},
    "seats": [{"id": "rowan", "display": "Rowan", "kind": "human"}],
}

#: Tokens from the vocabulary that was cut in 0.2.0. If any of these reappear
#: in player-facing text, the non-goal has drifted back.
DEAD_TOKENS = ["!huh", "!wait", "!yes", "!drag", "!mine", "!off",
               "!slow", "!landed", "!mymoment", "!contradicts"]

#: Files a player or a prospective user reads. Operator docs and source
#: comments may discuss the cut vocabulary as history; these may not present
#: one as usable.
PLAYER_FACING = ["README.md", "llms.txt", "tool.json",
                 os.path.join("docs", "QUICKSTART.md")]


class TestNoPlayerCommandSurface(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.led = Ledger(os.path.join(self.dir, "s.jsonl"))
        self.cfg = TableConfig(dict(CFG, data_dir=self.dir))

    def test_player_text_is_never_parsed_as_syntax(self):
        """Whatever a player types is dialogue. Ingest records that they
        spoke; it does not read their words for commands."""
        for text in ("!drag", "ok !drag can we move", "!yes", "!!huh",
                     "/pacing", "!roll 1d20", "!mine"):
            ingest.ingest_message(self.cfg, self.led,
                                  {"author": "Rowan", "content": text})
        self.assertEqual(self.led.read(lane="uxr"), [])

    def test_ingest_module_exposes_no_parser(self):
        for gone in ("parse", "strip", "MARKERS", "help_text"):
            self.assertFalse(hasattr(uxr, gone),
                             f"uxr.{gone} is a player-syntax surface and must stay removed")

    def test_no_markers_command_on_the_cli(self):
        self.assertNotIn("markers", cli.COMMANDS)

    def test_signals_are_internal_buckets_not_typed_tokens(self):
        """The buckets survive; the syntax does not. A bucket name must never
        be documented with a leading bang."""
        for name in uxr.SIGNALS:
            self.assertFalse(name.startswith("!"))
            self.assertNotIn("!", uxr.SIGNALS[name]["means"])

    def test_a_signal_requires_evidence(self):
        """Inference without the player's own words cannot be audited by the
        people who were actually in the room."""
        with self.assertRaises(uxr.SignalError):
            uxr.record_signal(self.led, "rowan", "pacing", "")

    def test_inferred_signals_can_never_be_defects(self):
        rec = uxr.record_signal(self.led, "rowan", "pacing",
                                "can we get moving", source="dm")
        self.assertNotEqual(rec.get("severity"), "defect")
        self.assertEqual(self.led.read(etype="qc.finding"), [])

    def test_player_facing_docs_advertise_no_tokens(self):
        for rel in PLAYER_FACING:
            path = os.path.join(ROOT, rel)
            if not os.path.exists(path):
                continue
            with open(path) as f:
                text = f.read()
            for tok in DEAD_TOKENS:
                self.assertNotIn(
                    tok, text,
                    f"{rel} advertises {tok} — players are never given a "
                    "command syntax (see tablekit/uxr.py)")

    def test_machine_descriptors_carry_no_stale_marker_language(self):
        """tool.json and llms.txt describe the CURRENT surface to agents and
        indexers. Prose about the removed vocabulary there is not history, it
        is a wrong description of what the tool does. (Prose docs may discuss
        it — saying "there is no marker card" is the point.)"""
        for rel in ("tool.json", "llms.txt"):
            with open(os.path.join(ROOT, rel)) as f:
                text = f.read().lower()
            self.assertNotIn("marker", text,
                             f"{rel} still describes the cut marker vocabulary")

    def test_docs_do_not_instruct_players_to_type_anything(self):
        """A softer net for phrasings that invite syntax without a bang."""
        bad = re.compile(r"players? (?:can |should |may )?type", re.I)
        for rel in PLAYER_FACING:
            path = os.path.join(ROOT, rel)
            if not os.path.exists(path):
                continue
            with open(path) as f:
                text = f.read()
            self.assertIsNone(
                bad.search(text),
                f"{rel} tells players to type something — the table speaks "
                "English, not syntax")


if __name__ == "__main__":
    unittest.main()
