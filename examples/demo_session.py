#!/usr/bin/env python3
"""A synthetic session, end to end, with no chat platform involved.

Run it to see what the instrumentation produces before you wire anything up:

    python3 examples/demo_session.py

The session below is deliberately imperfect. It contains the two failures that
this kit exists because of — an agent seat cued without its literal mention,
and a seat left unaddressed for most of an hour while the rest of the table
carried a scene — plus a called roll nobody consumed and a handful of player
markers. A demo where everything goes well would not show you anything.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tablekit import detector, pairs, report  # noqa: E402
from tablekit.config import TableConfig  # noqa: E402
from tablekit.events import Ledger  # noqa: E402
from tablekit import uxr  # noqa: E402

CONFIG = {
    "name": "The Example Table",
    "gm": {"id": "gm", "display": "GM"},
    "seats": [
        {"id": "rowan", "display": "Rowan", "kind": "human"},
        {"id": "brae", "display": "Brae", "kind": "human"},
        {"id": "vesh", "display": "Vesh", "kind": "agent",
         "mention": "<@999000111222333444>"},
    ],
}


def beat(led, t, text, cue=None, chunks=1):
    led.append("ux.beat", ts=t, words=len(text.split()), chunks=chunks,
               cued_seat=cue, text=text)
    led.append("qa.post", ts=t, ok=True, chars=len(text), chunks=chunks,
               latency_ms=90 + (len(text) % 60))
    if cue:
        pid = f"cue-{int(t)}"
        pairs.open_pair(led, "cue", pid, seat=cue, detail=text[:80], ts=t)
        return pid
    return None


def says(led, t, seat, text, signal=None, correlation_id=None):
    """A seat speaks. `signal` is what the GM understood from it — recorded
    with their actual words attached, never parsed out of a token."""
    led.append("qa.inbound", ts=t, seat=seat, chars=len(text),
               words=len(text.split()))
    if signal:
        uxr.record_signal(led, seat, signal, text, source="dm", ts=t)
    pairs.close_one(led, ("cue",), seat, {"cue": "taken"},
                    correlation_id=correlation_id,
                    detail="demo inbound", ts=t)


def main():
    cfg = TableConfig(CONFIG)
    path = os.path.join(tempfile.mkdtemp(), "demo.jsonl")
    led = Ledger(path)
    t = time.time() - 4200  # a seventy-minute session that ended just now

    cue_id = beat(led, t, "The causeway stones are coming up out of the water one at a "
                          "time, and the bell above the chapel has not rung since you "
                          "got here. Rowan, you are first onto the wet stone.",
                  cue="rowan")
    says(led, t + 40, "rowan", "I go slow, watching the water line. Oh this is good.",
         signal="resonance", correlation_id=cue_id)

    t += 300
    cue_id = beat(led, t, "Halfway across, something under the surface keeps pace with "
                          "you. Brae?", cue="brae")
    says(led, t + 55, "brae", "I hold up a hand and stop the group.",
         correlation_id=cue_id)

    t += 240
    # A roll called and never consumed — the ledger defect this catches.
    beat(led, t, "Give me a Perception check to place the sound.")
    pairs.open_pair(led, "roll", "roll-1", seat="brae",
                    detail="Perception to place the sound", ts=t)
    says(led, t + 30, "brae", "14?")

    t += 200
    # The cue that will never be delivered: agent seat, no literal mention.
    beat(led, t, "Vesh, the water has gone quiet around your ankles. What do "
                 "you do?", cue="vesh")

    t += 420
    cue_id = beat(led, t, "The stones behind you are already under. Rowan, you can see "
                          "the chapel door standing open.", cue="rowan")
    says(led, t + 60, "rowan", "I run for it — finally, this is what the lantern is for.",
         signal="spotlight", correlation_id=cue_id)

    t += 380
    beat(led, t, "Inside, thirty-one cuts in the doorframe, and the rope is "
                 "still swinging. The air smells like low tide and old iron, "
                 "and every one of those cuts is a name someone stopped "
                 "saying out loud. There is a ledger on the sill, open, and "
                 "the last entry is today's date in a hand none of you know, "
                 "and below that a line of gorse pressed flat between the "
                 "pages, and the ink is not dry.", chunks=3)
    says(led, t + 70, "brae", "wait, what's a gorse?", signal="comprehension")
    says(led, t + 90, "rowan", "is anything happening here", signal="pacing")

    t += 500
    beat(led, t, "The rope stops swinging.")
    says(led, t + 40, "brae", "can we get moving", signal="pacing")
    says(led, t + 60, "rowan", "I read the last entry aloud. Are we done here?",
         signal="pacing")

    # A QC pass mid-session, which is when these checks actually run — between
    # beats, while there is still time to fix anything. This is where Vesh's
    # silence gets caught: forty minutes unaddressed while the table carried
    # the scene, with the undeliverable cue at beat 4 as the cause.
    detector.record(led, detector.evaluate(led, cfg, now=t + 90))

    t += 300
    beat(led, t, "It is your own name, Rowan.")
    says(led, t + 30, "rowan", "okay. Okay, that's a moment.", signal="resonance")
    says(led, t + 45, "brae", "hold on — we never told anyone our names here",
         signal="continuity")

    # The close: ask, in plain English, and record what they say. This is the
    # half that checks the GM's own blind spots.
    uxr.record_debrief(led, "brae", "anything drag tonight?",
                       "the chapel description went long, I lost the thread",
                       signal="pacing", ts=t + 700)
    uxr.record_debrief(led, "rowan", "anything stick with you?",
                       "the name in the ledger. I did not see that coming",
                       signal="resonance", ts=t + 720)

    # End of the evening: expire what is still hanging.
    now = t + 800
    pairs.sweep(led, now=now, ttls=cfg.thresholds)
    beat(led, now, "The chapel door closes behind you. That is where we stop.")
    detector.record(led, detector.evaluate(led, cfg, now=now))

    rep = report.build(led, cfg)
    print(report.render(rep))
    print(f"\n(session file: {path})")
    return report.exit_code(rep)


if __name__ == "__main__":
    sys.exit(main())
