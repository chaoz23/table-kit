"""The elicited lane — the only instrument that crosses the legibility boundary.

Everything else this kit records can be reconstructed from the session file
afterwards. This lane cannot, because it measures things that leave no trace:

  * whether a description landed or slid past
  * whether a word was understood
  * whether a player wanted the floor and did not get it
  * whether the evening was dragging *for them* while it felt brisk to the GM
  * whether someone got to do the thing their character exists to do

A transcript of a great session and a transcript of a session everyone endured
look about the same. So the kit asks, in the cheapest form that will actually
get used mid-play: a single token dropped into the chat.

## The vocabulary

Six markers. Each maps to one craft dimension, and each was chosen because a
real table lost information for the want of it.

| marker  | the seat is saying                          | dimension            |
|---------|---------------------------------------------|----------------------|
| `!huh`  | I did not follow that                       | comprehension        |
| `!wait` | I wanted in and the moment passed            | floor / seat access  |
| `!yes`  | that landed                                  | what to do more of   |
| `!drag` | this is slow *for me* right now              | pacing               |
| `!mine` | I got to do my character's particular thing  | spotlight fit        |
| `!off`  | that contradicts something established       | continuity           |

`!yes` and `!mine` matter as much as the complaints. An instrument that only
collects grievances will teach a GM to avoid risk, which is a different thing
from teaching them to run a good table.

## The rule that keeps this honest

**A marker is evidence for a pattern. It is never a verdict on a beat.**

This is not politeness, it is a correction of a real error. A single `!huh`
was once read as "unfamiliar words fail at this table," and the rule written
from it was wrong — the player's actual position was "I like the new words,
sometimes I will have to ask." One report of friction tells you a moment had
friction. It does not tell you the cause, and it certainly does not generalise
to a preference. So the reporting layer refuses to describe anything below
`MIN_PATTERN` occurrences as a rate or a tendency; it lists the individual
moments and leaves the inference to whoever was in the room.
"""

import re

#: Below this many observations of a marker, the report shows the individual
#: moments and refuses to characterise a tendency. Three is not a statistical
#: threshold — no N this small is — it is the smallest number that cannot be a
#: single bad moment plus noise.
MIN_PATTERN = 3

MARKERS = {
    "huh":  {"meaning": "did not follow that",
             "dimension": "comprehension",
             "ask": "what was unclear — a word, or what was happening?"},
    "wait": {"meaning": "wanted in and the moment passed",
             "dimension": "floor access",
             "ask": "what were you about to do?"},
    "yes":  {"meaning": "that landed",
             "dimension": "resonance",
             "ask": "what landed, specifically?"},
    "drag": {"meaning": "slow for me right now",
             "dimension": "pacing",
             "ask": "were you waiting on someone, or on the scene?"},
    "mine": {"meaning": "got to do my character's particular thing",
             "dimension": "spotlight fit",
             "ask": None},
    "off":  {"meaning": "contradicts something established",
             "dimension": "continuity",
             "ask": "what do you remember differently?"},
}

#: `!huh` anywhere in a line, optionally trailed by a note up to end of line.
#: Deliberately permissive about position: players type these mid-sentence,
#: and a marker that only works at the start of a line will not be used.
_RX = re.compile(r"(?<![\w!])!(" + "|".join(MARKERS) + r")\b[ \t:,-]*(.*)$",
                 re.IGNORECASE | re.MULTILINE)


def parse(text):
    """Extract markers from one chat message.

    Returns a list of `{"marker": str, "note": str|None}`. A message with no
    markers returns `[]`, which is the overwhelmingly common case — this is
    called on every inbound line.
    """
    found = []
    for m in _RX.finditer(text or ""):
        note = (m.group(2) or "").strip() or None
        # A note that is itself just another marker belongs to that marker.
        if note and note.startswith("!"):
            note = None
        found.append({"marker": m.group(1).lower(), "note": note})
    return found


def strip(text):
    """The message with its markers removed.

    Used so a marker dropped mid-sentence does not end up quoted back to the
    table as if it were dialogue.
    """
    return _RX.sub("", text or "").strip()


def record(ledger, seat, text, ts=None):
    """Parse `text` and append one `uxr.marker` event per marker found.

    Returns the events written. Anchors each to the current beat so friction
    points at a cause, not a clock reading.
    """
    beat = ledger.current_beat()
    out = []
    for f in parse(text):
        out.append(ledger.append("uxr.marker", ts=ts, seat=seat,
                                 marker=f["marker"], note=f["note"], beat=beat))
    return out


# ---------------------------------------------------------------------------
# User stories
# ---------------------------------------------------------------------------

#: Templates keyed by marker. The classic user-story form, filled from what the
#: record actually contains — never invented. `{seat}`, `{note}`, `{beat}`.
_STORY = {
    "wait": "As {seat}, I wanted to act on what was happening, but the floor "
            "moved on before I got it (beat {beat}).",
    "huh":  "As {seat}, I wanted to follow the scene, but something in it did "
            "not parse (beat {beat}).",
    "drag": "As {seat}, I wanted the scene to keep moving, but it was slow "
            "from where I sat (beat {beat}).",
    "off":  "As {seat}, I wanted the world to stay consistent, but a detail "
            "contradicted what I had already been told (beat {beat}).",
    "yes":  "As {seat}, I got a moment that landed (beat {beat}).",
    "mine": "As {seat}, I got to do the thing my character is for (beat {beat}).",
}


def stories(markers, beat_text=None):
    """Turn marker events into user-story records.

    `beat_text` is an optional `{beat_index: text}` map used to attach the
    beat a marker was reacting to. Nothing is inferred: if the note is absent
    the story says so rather than guessing at a cause.
    """
    out = []
    for m in markers:
        marker = m.get("marker")
        if marker not in _STORY:
            continue
        beat = m.get("beat", 0)
        story = _STORY[marker].format(seat=m.get("seat", "a seat"), beat=beat)
        rec = {
            "seat": m.get("seat"),
            "marker": marker,
            "dimension": MARKERS[marker]["dimension"],
            "beat": beat,
            "story": story,
            "said": m.get("note"),
            "cause": "reported" if m.get("note") else "not stated",
        }
        if beat_text and beat in beat_text:
            rec["reacting_to"] = beat_text[beat]
        out.append(rec)
    return out


def followups(markers):
    """Questions worth asking at the break, deduplicated.

    Only for markers whose cause was NOT stated — if the player already said
    what was wrong, asking again is noise.
    """
    asked, out = set(), []
    for m in markers:
        marker = m.get("marker")
        info = MARKERS.get(marker)
        if not info or not info["ask"] or m.get("note"):
            continue
        key = (m.get("seat"), marker)
        if key in asked:
            continue
        asked.add(key)
        out.append({"seat": m.get("seat"), "marker": marker,
                    "beat": m.get("beat"), "question": info["ask"]})
    return out


def help_text():
    lines = ["Drop these in chat any time — one word, no need to stop play:", ""]
    for name, info in MARKERS.items():
        lines.append(f"  !{name:<5} {info['meaning']}")
    lines += ["",
              "Add a few words after one if you have them (`!huh what's a gorse`).",
              "They are read as signals for the GM to look at later, never as "
              "a complaint about the moment."]
    return "\n".join(lines)
