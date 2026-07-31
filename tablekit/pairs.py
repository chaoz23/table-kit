"""Outcome pairs — the only records that can say whether a craft move worked.

A session transcript is a record of *decisions*. It shows that the GM cued a
seat, called for a roll, checked on someone who had gone quiet. It does not
show whether any of that worked, because the payoff is separated from the move
by minutes and by other people's turns.

A pair fixes that by recording the two halves as one thing: an intent is
`open`ed with an id, and later `close`d with what actually happened. After a
few sessions you can ask questions no transcript can answer — do in-fiction
cues get taken more often than out-of-fiction ones at *this* table, do called
rolls get consumed, does checking on a quiet seat bring it back.

## The bar for being a pair

The payoff has to be observable by someone other than the person who made the
move. "Did the scene feel tense" is not a pair. "Did the seat act within five
minutes" is. Anything that fails this test belongs in the elicited lane, where
it is honestly labelled as self-report, or nowhere.

## Expiry is a real outcome

A pair that is never closed is not missing data — it is usually the finding.
A cue nobody answered and a roll nobody consumed both look like silence in the
file. `sweep()` converts them to `expired` and `unconsumed` explicitly, so the
absence gets counted instead of quietly reducing the denominator.
"""

import itertools
import os
import time

from .events import PAIR_KINDS

#: Guarantees uniqueness within a process. Randomness alone does not: with the
#: timestamp pinned, two random bytes collide inside a few hundred draws.
_SEQ = itertools.count()


def new_id(kind, ts=None):
    """A pair id that cannot collide with another opened in the same instant.

    Second-resolution ids looked fine until two beats went out inside one
    second at a live table — the second cue took the first one's id, and
    closing one closed both. Cue-uptake computed from that is quietly wrong
    rather than obviously broken, which is the worst way for an id scheme to
    fail.

    Three parts, each covering what the others cannot: milliseconds order the
    ids readably, the sequence makes collisions *impossible* within one
    process, and the random suffix keeps two concurrent processes — a CLI
    invocation and a listener — from landing on the same id.
    """
    ms = int((ts if ts is not None else time.time()) * 1000)
    return f"{kind}-{ms}-{next(_SEQ):04x}-{os.urandom(2).hex()}"


def open_pair(ledger, pair, pid, seat=None, detail=None, beat=None, ts=None,
              dc=None, rolled_by=None):
    """Open an intent.

    `dc` is recorded on roll pairs whether or not the number was ever said out
    loud. Stating a DC is a play decision; recording one is an instrumentation
    decision, and conflating them costs us the only thing that makes
    fail-forward and several other craft rules falsifiable at our own table.
    """
    if pair not in PAIR_KINDS:
        raise ValueError(f"unknown pair kind {pair!r}")
    return ledger.append("out.open", ts=ts, pair=pair, id=pid, seat=seat,
                         detail=detail, dc=dc, rolled_by=rolled_by,
                         beat=beat if beat is not None else ledger.current_beat())


def close_pair(ledger, pair, pid, outcome, detail=None, ts=None, opened_ts=None):
    latency = None
    if opened_ts is None:
        for rec in ledger.read(etype="out.open"):
            if rec.get("id") == pid and rec.get("pair") == pair:
                opened_ts = rec.get("ts")
    if opened_ts is not None:
        latency = round(float(ts if ts is not None else time.time()) - opened_ts, 2)
    return ledger.append("out.close", ts=ts, pair=pair, id=pid, outcome=outcome,
                         detail=detail, latency_s=latency)


def pairs(ledger):
    """All pairs as `{id: {...}}`, closed or not."""
    out = {}
    for rec in ledger.read(lane="out"):
        pid = rec.get("id")
        if rec["type"] == "out.open":
            out[pid] = {"id": pid, "pair": rec.get("pair"), "seat": rec.get("seat"),
                        "detail": rec.get("detail"), "beat": rec.get("beat"),
                        "opened_ts": rec.get("ts"), "outcome": None,
                        "latency_s": None, "closed_ts": None}
        elif pid in out:
            out[pid].update(outcome=rec.get("outcome"),
                            latency_s=rec.get("latency_s"),
                            closed_ts=rec.get("ts"),
                            close_detail=rec.get("detail"))
    return out


def open_now(ledger, kind=None):
    return [p for p in pairs(ledger).values()
            if p["outcome"] is None and (kind is None or p["pair"] == kind)]


#: What an un-closed pair of each kind means once its time is up. These are
#: outcomes, not errors — see the module docstring.
EXPIRY = {"cue": "expired", "roll": "unconsumed", "checkin": "absent",
          "endmarker": "diverged"}


def sweep(ledger, now=None, ttls=None):
    """Close pairs that have outstayed their time-to-live.

    Call between beats and at session close. Returns the events written.
    """
    now = now if now is not None else time.time()
    ttls = ttls or {}
    written = []
    for p in open_now(ledger):
        ttl = ttls.get(f"{p['pair']}_ttl_s", ttls.get(p["pair"]))
        if not ttl or p["opened_ts"] is None:
            continue
        if now - p["opened_ts"] > ttl:
            written.append(close_pair(
                ledger, p["pair"], p["id"], EXPIRY.get(p["pair"], "expired"),
                detail=f"no resolution within {int(ttl)}s", ts=now,
                opened_ts=p["opened_ts"]))
    return written


#: Outcomes that mean the move did what it was for. Everything else did not,
#: and `None` means we still do not know — which is a third state the summary
#: keeps separate rather than folding into failure.
GOOD = {"taken", "consumed", "returned", "matched"}


def summary(ledger, min_n=3):
    """Per-kind outcome counts and latency, with a hard floor on rates.

    Below `min_n` closed pairs of a kind, the rate is withheld and the raw
    counts are shown instead. A "0% cue uptake" computed from one cue is not a
    finding about the table; it is a finding about the sample.
    """
    by_kind = {}
    for p in pairs(ledger).values():
        k = by_kind.setdefault(p["pair"], {"opened": 0, "closed": 0, "good": 0,
                                           "outcomes": {}, "latencies": []})
        k["opened"] += 1
        if p["outcome"] is None:
            continue
        k["closed"] += 1
        k["outcomes"][p["outcome"]] = k["outcomes"].get(p["outcome"], 0) + 1
        if p["outcome"] in GOOD:
            k["good"] += 1
        if p["latency_s"] is not None:
            k["latencies"].append(p["latency_s"])
    for k, v in by_kind.items():
        lat = sorted(v.pop("latencies"))
        v["median_latency_s"] = lat[len(lat) // 2] if lat else None
        v["still_open"] = v["opened"] - v["closed"]
        if v["closed"] >= min_n:
            v["success_rate"] = round(v["good"] / v["closed"], 3)
        else:
            v["success_rate"] = None
            v["note"] = (f"only {v['closed']} closed — too few to state a rate; "
                         "counts shown instead")
    return by_kind
