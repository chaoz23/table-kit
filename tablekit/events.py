"""The event schema — one append-only stream for a live table.

A table run emits exactly ONE file. It is simultaneously:

  * the **play ledger** — `turn` / `act` / `event` records in the shape
    `dmcheck` already reads, so a conduct checker can be pointed straight at
    it with no export step; and
  * the **instrumentation stream** — four lanes of telemetry about the run
    itself, which `dmcheck` ignores because it does not recognise the types.

Keeping them in one file is the point. A separate telemetry file drifts from
the ledger the moment anything crashes, and then you are reconciling two
partial accounts of the same evening.

## The four instrumentation lanes

| lane  | question it answers            | source                   |
|-------|--------------------------------|--------------------------|
| `qa`  | did the machinery work?        | the kit, automatically   |
| `qc`  | was the refereeing correct?    | checks, automatically    |
| `ux`  | what was the seat's experience?| timings, automatically   |
| `uxr` | how did it *feel* from a seat? | **elicited from players**|

The split between `ux` and `uxr` is not cosmetic. Everything in `ux` is
derivable from the record afterwards. Nothing in `uxr` is: whether a beat
landed, whether a player was bored, whether a word was understood, and
whether someone got to do the thing their character is *for* leave no trace
in a transcript. They have to be asked for while the table is still sitting
there, which is why the marker vocabulary is one token long.

A fifth lane, `out`, records outcome PAIRS — an intent opened and later
closed with what actually happened. Those are the only records in the file
that can say whether a craft move worked.
"""

import json
import os
import tempfile
import time

LANES = ("play", "qa", "qc", "ux", "uxr", "out")

# Play types are fixed by dmcheck's ledger reader. Do not extend this tuple
# without checking `dmcheck.core.load_ledger` — an unrecognised play type is
# silently ignored there, which looks exactly like a table that never acted.
PLAY_TYPES = ("turn", "act", "event")

# type -> required keys (beyond ts/type). Unknown types are refused at write
# time: a typo that lands in the file as an unreadable record is worse than a
# crash, because it reads as an absence later.
SCHEMA = {
    # --- play (dmcheck-compatible) ------------------------------------
    "turn":  ("actor",),
    "act":   ("actor", "text"),
    "event": ("text",),
    # --- qa: does the machinery work ----------------------------------
    "qa.post":       ("ok", "chars", "chunks"),
    "qa.post_failed": ("error",),
    "qa.inbound":    ("seat", "chars"),
    "qa.listener":   ("state",),
    "qa.command":    ("cmd", "ok"),
    # --- qc: is the refereeing correct --------------------------------
    "qc.finding": ("check", "detail"),
    "qc.pass":    ("checks",),
    "qc.mark":    ("narrated_through",),
    # --- ux: objective seat experience --------------------------------
    "ux.beat":      ("words", "chunks"),
    "ux.turn":      ("seat", "wait_s"),
    "ux.seat_idle": ("seat", "idle_s"),
    # --- uxr: elicited, subjective ------------------------------------
    "uxr.marker":  ("seat", "marker"),
    "uxr.debrief": ("seat", "question", "answer"),
    # --- out: outcome pairs (E5) --------------------------------------
    "out.open":  ("pair", "id"),
    "out.close": ("pair", "id", "outcome"),
}

# Pair kinds. Each is an intent whose payoff is observable, which is the whole
# bar for entry: if nobody can tell afterwards whether it worked, it is not a
# pair, it is an opinion.
PAIR_KINDS = {
    "cue":       "GM addressed a seat; did that seat act, and how fast?",
    "roll":      "GM called for a roll; was a result produced and consumed?",
    "checkin":   "a quiet seat was checked on; did it come back?",
    "endmarker": "a session end marker; did the next opening match it?",
}

PAIR_OUTCOMES = ("taken", "ignored", "expired", "superseded", "consumed",
                 "unconsumed", "returned", "absent", "matched", "diverged")


class SchemaError(ValueError):
    """Refused at write time. See the module docstring for why this is loud."""


def lane_of(etype):
    return etype.split(".", 1)[0] if "." in etype else "play"


def validate(rec):
    """Return `rec` unchanged, or raise SchemaError.

    Deliberately strict about the four known-required things and silent about
    everything else — extra keys are how a table adds its own context without
    forking the schema.
    """
    etype = rec.get("type")
    if etype not in SCHEMA:
        raise SchemaError(
            f"unknown event type {etype!r}; known types: {', '.join(sorted(SCHEMA))}")
    if not isinstance(rec.get("ts"), (int, float)):
        raise SchemaError(f"{etype}: ts must be an epoch float")
    missing = [k for k in SCHEMA[etype] if k not in rec]
    if missing:
        raise SchemaError(f"{etype}: missing required key(s) {', '.join(missing)}")
    if etype in ("out.open", "out.close") and rec["pair"] not in PAIR_KINDS:
        raise SchemaError(
            f"unknown pair kind {rec['pair']!r}; known: {', '.join(sorted(PAIR_KINDS))}")
    if etype == "out.close" and rec["outcome"] not in PAIR_OUTCOMES:
        raise SchemaError(
            f"unknown outcome {rec['outcome']!r}; known: {', '.join(PAIR_OUTCOMES)}")
    return rec


def make(etype, ts=None, **fields):
    """Build and validate one event."""
    rec = {"ts": float(ts if ts is not None else time.time()), "type": etype}
    rec.update({k: v for k, v in fields.items() if v is not None})
    return validate(rec)


class Ledger:
    """Append-only JSONL. One per session; never rewritten in place.

    Append is a single `write()` of one line opened in append mode, which is
    atomic enough for the concurrent writers a table actually has (a listener
    process and a CLI). Nothing here holds the file open across beats — a
    session that survives a crashed listener is worth more than a buffered
    write.
    """

    def __init__(self, path):
        self.path = str(path)

    # ---- writing ----------------------------------------------------
    def append(self, etype, **fields):
        rec = make(etype, **fields)
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    # ---- reading ----------------------------------------------------
    def read(self, lane=None, etype=None):
        """All records, optionally filtered. Malformed lines are surfaced as
        `_malformed` records rather than dropped — a silently skipped line is
        the same failure mode as an unknown type."""
        out = []
        if not os.path.exists(self.path):
            return out
        with open(self.path) as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    out.append({"ts": 0.0, "type": "_malformed",
                                "line": n, "error": str(e)})
                    continue
                if lane and lane_of(rec.get("type", "")) != lane:
                    continue
                if etype and rec.get("type") != etype:
                    continue
                out.append(rec)
        return out

    def beats(self):
        return self.read(etype="ux.beat")

    def current_beat(self):
        """Index of the most recent GM beat, or 0 before the first one.

        UXR markers anchor to this so friction points at a cause instead of a
        clock reading.
        """
        return len(self.beats())

    def last(self, etype=None, lane=None):
        rows = self.read(lane=lane, etype=etype)
        return rows[-1] if rows else None


def write_atomic(path, text):
    """Used for derived artifacts (reports), never for the ledger itself."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
