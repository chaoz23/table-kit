"""The elicited lane — what a session was *like*, which no transcript holds.

Everything else this kit records can be reconstructed from the session file
afterwards. This lane cannot, because it measures things that leave no trace:

  * whether a description landed or slid past
  * whether a player was following, or had quietly lost the thread
  * whether someone wanted the floor and the moment closed
  * whether the evening dragged *for them* while it felt brisk to the GM
  * whether someone got to do the thing their character exists to do

A transcript of a great session and a transcript of a session everyone endured
look about the same.

## The product non-goal that shapes this whole module

**Players are never given a command syntax.** No `!tokens`, no aliases, no
keywords to memorise — not as a convenience, not as an "advanced" option, not
hidden in the docs. This is a hard product constraint, not a preference, and
`tests/test_no_player_commands.py` enforces it.

The reason is the top-level goal. Text RPGs solved this problem badly for
forty years: Zork and *Moria* and everything after them gave players a verb
list because a parser in 1980 could not understand a sentence. The command
vocabulary was never a design choice, it was a workaround for a missing
capability — and it is exactly what makes those games feel like operating a
computer rather than sitting with a person who is running a world for you.

That capability is no longer missing. An agent GM that hands players a token
vocabulary has voluntarily reproduced the limitation, and traded away the only
thing that makes it worth playing with over a parser game.

It would not even work on its own terms: a labelled, on-the-record `!drag` is
*more* socially expensive than muttering "ooh can we get to the fight", not
less. The players who most need the channel are the ones who would never use
it.

So the buckets below are **internal steering categories**, inferred in the
background from what people already say. They are for the GM and the report.
They are never surfaced to a player as something to type. The rule generalises
past this module: anywhere the table has to speak computer instead of speaking
English is the same failure. GM-side tooling (`tablekit signal`, the CLI) is
operator machinery and exempt — nobody at the table types it.

## Where signals come from

| source | what it is | weight |
|---|---|---|
| `dm` | the agent GM classifying a line it already read | advisory |
| `local` | an independent model pass over the same transcript | advisory, and a check on `dm`'s blind spots |
| `debrief` | a plain-English question asked at session close, answered | the high-confidence channel |

`dm` alone is not enough on its own, and the reason is structural: a GM that
does not notice friction will not record friction, so the instrument inherits
exactly the blind spots the lane was built to route around. That is what the
`local` pass and the debrief are for. See docs/INSTRUMENTATION.md.

## The honesty fences

**Inferred signals never become defects.** They are advisory in every path.
Something a model *thought* a player meant cannot be allowed to accuse.

**The player's own words are always stored with an inferred signal**, so the
inference is auditable by whoever was in the room. A classification with no
quote is refused.

**A signal is evidence for a pattern, never a verdict on a beat.** Below
`MIN_PATTERN`, the report lists individual moments and refuses to state a rate
or a tendency. This is a correction of a real error: one player asking what a
word meant was once generalised into "unfamiliar diction fails at this table",
and their actual position was the opposite — *"I like the new words, sometimes
I will have to ask."*
"""

#: Below this many observations, the report shows individual moments and
#: refuses to characterise a tendency. Three is not a statistical threshold —
#: no N this small is — it is the smallest number that cannot be a single bad
#: moment plus noise.
MIN_PATTERN = 3

#: Internal steering buckets. NOT a player-facing vocabulary — see the module
#: docstring. `cues` are illustrative of what the classification is looking
#: for in ordinary speech; they are documentation for whoever writes the
#: classifier prompt, never a pattern-matching table.
SIGNALS = {
    "pacing": {
        "means": "this stretch felt slow from that seat",
        "cues": ["can we get moving", "are we done here", "so anyway",
                 "is anything happening"],
        "ask": "was anything dragging tonight — waiting on someone, "
               "or waiting on the scene?",
    },
    "floor": {
        "means": "wanted to act and the moment closed first",
        "cues": ["oh I was going to", "never mind then", "I was about to say",
                 "wait, can I still"],
        "ask": "was there a moment you wanted in on and did not get?",
    },
    "comprehension": {
        "means": "lost the thread — a word, or what is happening",
        "cues": ["what's a", "wait, where are we", "who is that again",
                 "sorry, what just happened"],
        "ask": "anything you were unclear on — where you were, or what "
               "something meant?",
    },
    "resonance": {
        "means": "that landed",
        "cues": ["oh that's good", "okay that's a moment", "brilliant",
                 "I love that"],
        "ask": "anything stick with you from tonight?",
    },
    "spotlight": {
        "means": "got to do the thing this character is for",
        "cues": ["finally", "this is what she does", "been waiting to use"],
        "ask": "did you get to do the thing you wanted with your character?",
    },
    "continuity": {
        "means": "contradicts something already established",
        "cues": ["I thought we", "didn't you say", "that's not what",
                 "we never told anyone"],
        "ask": "did anything contradict what you already knew?",
    },
}

#: Signals that mean something went well. Kept explicit so the report cannot
#: quietly become a grievance log — an instrument that only collects complaints
#: teaches a GM to avoid risk, which is a different thing from running a good
#: table.
POSITIVE = {"resonance", "spotlight"}

SOURCES = ("dm", "local", "debrief")


class SignalError(ValueError):
    pass


def record_signal(ledger, seat, signal, quote, source="dm", beat=None,
                  note=None, ts=None):
    """Record one inferred signal.

    `quote` is the player's own words, verbatim, and it is **required**. An
    inferred signal without the evidence it was inferred from cannot be
    checked by anyone, which makes it indistinguishable from an opinion the
    machine made up.
    """
    if signal not in SIGNALS:
        raise SignalError(
            f"unknown signal {signal!r}; known: {', '.join(sorted(SIGNALS))}")
    if source not in SOURCES:
        raise SignalError(
            f"unknown source {source!r}; known: {', '.join(SOURCES)}")
    if not (quote or "").strip():
        raise SignalError(
            f"{signal}: a quote is required — an inferred signal with no "
            "evidence cannot be audited by whoever was in the room")
    return ledger.append(
        "uxr.signal", ts=ts, seat=seat, signal=signal, quote=quote.strip()[:300],
        source=source, note=note,
        beat=beat if beat is not None else ledger.current_beat())


def record_debrief(ledger, seat, question, answer, signal=None, ts=None):
    """Record one plain-English question and its answer at session close."""
    if signal is not None and signal not in SIGNALS:
        raise SignalError(f"unknown signal {signal!r}")
    return ledger.append("uxr.debrief", ts=ts, seat=seat, question=question,
                         answer=answer, signal=signal)


def debrief_questions(ledger=None, seats=None):
    """The questions worth asking at session close, in plain English.

    Ordered so the two that get honest answers first — the positive ones —
    come before the complaints. People warm up.
    """
    order = ["resonance", "spotlight", "pacing", "floor", "comprehension",
             "continuity"]
    return [{"signal": s, "question": SIGNALS[s]["ask"]} for s in order]


# ---------------------------------------------------------------------------
# User stories
# ---------------------------------------------------------------------------

_STORY = {
    "floor": "As {seat}, I wanted to act on what was happening, but the "
             "moment closed before I got it (beat {beat}).",
    "comprehension": "As {seat}, I wanted to follow the scene, but I had lost "
                     "the thread (beat {beat}).",
    "pacing": "As {seat}, I wanted the scene to keep moving, but it was slow "
              "from where I sat (beat {beat}).",
    "continuity": "As {seat}, I wanted the world to stay consistent, but a "
                  "detail contradicted what I had already been told "
                  "(beat {beat}).",
    "resonance": "As {seat}, I got a moment that landed (beat {beat}).",
    "spotlight": "As {seat}, I got to do the thing my character is for "
                 "(beat {beat}).",
}


def stories(signals, beat_text=None):
    """Turn signal events into user-story records.

    Nothing is invented. Every story carries the words it was inferred from
    and the source that inferred it, so a reader can disagree with the
    classification without having to trust it.
    """
    out = []
    for s in signals:
        kind = s.get("signal")
        if kind not in _STORY:
            continue
        beat = s.get("beat", 0)
        rec = {
            "seat": s.get("seat"),
            "signal": kind,
            "means": SIGNALS[kind]["means"],
            "beat": beat,
            "story": _STORY[kind].format(seat=s.get("seat") or "a seat",
                                         beat=beat),
            "said": s.get("quote"),
            "source": s.get("source", "dm"),
            "positive": kind in POSITIVE,
        }
        if beat_text and beat in beat_text:
            rec["reacting_to"] = beat_text[beat]
        out.append(rec)
    return out
