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

import time
import uuid

from .events import PAIR_KINDS, PAIR_OUTCOMES, SchemaError


class PairError(SchemaError):
    """Typed refusal for an invalid obligation lifecycle transition."""

    def __init__(self, code, message, **details):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.details = details


def new_id(kind, ts=None):
    """Return a readable, globally collision-resistant obligation ID.

    The timestamp is diagnostic only. UUID4 supplies 122 random bits across
    processes and hosts; ``open_pair`` still enforces ledger uniqueness so an
    injected/test collision fails instead of overwriting state.
    """
    if kind not in PAIR_KINDS:
        raise PairError("unknown_pair_kind", f"unknown pair kind {kind!r}")
    ms = int((ts if ts is not None else time.time()) * 1000)
    return f"{kind}-{ms}-{uuid.uuid4().hex}"


def open_pair(ledger, pair, pid, seat=None, detail=None, beat=None, ts=None,
              dc=None, rolled_by=None):
    """Open an intent.

    `dc` is recorded on roll pairs whether or not the number was ever said out
    loud. Stating a DC is a play decision; recording one is an instrumentation
    decision, and conflating them costs us the only thing that makes
    fail-forward and several other craft rules falsifiable at our own table.
    """
    if pair not in PAIR_KINDS:
        raise PairError("unknown_pair_kind", f"unknown pair kind {pair!r}")
    if not isinstance(pid, str) or not pid.strip():
        raise PairError("invalid_pair_id", "pair id must be a non-empty string")
    # Validate existing lifecycle state before attempting the atomic insert.
    pairs(ledger)
    rec, created = ledger.append_once(
        "out.open", {"id": pid}, ts=ts, pair=pair, id=pid, seat=seat,
        detail=detail, dc=dc, rolled_by=rolled_by,
        beat=beat if beat is not None else ledger.current_beat())
    if not created:
        raise PairError(
            "duplicate_pair_id", f"pair id {pid!r} is already open or used",
            pair_id=pid)
    return rec


def close_pair(ledger, pair, pid, outcome, detail=None, ts=None, opened_ts=None,
               source=None, source_id=None, evidence=None):
    state = pairs(ledger)
    current = state.get(pid)
    if current is None:
        raise PairError("orphan_close", f"pair id {pid!r} was never opened",
                        pair_id=pid)
    if current["pair"] != pair:
        raise PairError(
            "pair_kind_mismatch",
            f"pair id {pid!r} is {current['pair']!r}, not {pair!r}",
            pair_id=pid, expected=current["pair"], received=pair)
    if current["outcome"] is not None:
        raise PairError(
            "pair_already_closed",
            f"pair id {pid!r} already closed as {current['outcome']!r}",
            pair_id=pid, outcome=current["outcome"])
    allowed = PAIR_OUTCOMES[pair]
    if outcome not in allowed:
        raise PairError(
            "invalid_pair_outcome",
            f"outcome {outcome!r} is invalid for {pair!r}; allowed: "
            f"{', '.join(allowed)}",
            pair_id=pid, pair=pair, outcome=outcome)
    if evidence is None:
        evidence = {}
    if not isinstance(evidence, dict):
        raise PairError("invalid_resolution_evidence",
                        "resolution evidence must be an object")
    if any(not isinstance(key, str) for key in evidence):
        raise PairError("invalid_resolution_evidence",
                        "resolution evidence keys must be strings")
    reserved = {"ts", "type", "pair", "id", "outcome", "detail",
                "latency_s", "source", "source_id"} & set(evidence)
    if reserved:
        raise PairError(
            "invalid_resolution_evidence",
            f"resolution evidence cannot replace {', '.join(sorted(reserved))}")
    latency = None
    actual_opened_ts = current.get("opened_ts")
    if opened_ts is not None and opened_ts != actual_opened_ts:
        raise PairError(
            "opener_mismatch",
            f"pair id {pid!r} supplied an opening timestamp that does not "
            "match its immutable opener", pair_id=pid)
    opened_ts = actual_opened_ts
    if opened_ts is not None:
        closed_at = float(ts if ts is not None else time.time())
        if closed_at < opened_ts:
            raise PairError(
                "resolution_precedes_open",
                f"pair id {pid!r} cannot close before it opened", pair_id=pid)
        latency = round(closed_at - opened_ts, 2)
    rec, created = ledger.append_once(
        "out.close", {"id": pid}, ts=ts, pair=pair, id=pid, outcome=outcome,
        detail=detail, latency_s=latency, source=source, source_id=source_id,
        **evidence)
    if not created:
        raise PairError(
            "pair_already_closed", f"pair id {pid!r} already has a resolution",
            pair_id=pid)
    return rec


def pairs(ledger):
    """All pairs as ``{id: {...}}`` or a typed lifecycle refusal.

    Duplicate opens, orphan/reordered closes, kind changes, and second closes
    are not folded into plausible state.  A ledger with any of those cannot
    safely answer which obligation remains open.
    """
    out = {}
    for rec in ledger.read(lane="out"):
        pid = rec.get("id")
        if rec["type"] == "out.open":
            if pid in out:
                raise PairError(
                    "duplicate_pair_id", f"pair id {pid!r} was opened twice",
                    pair_id=pid)
            out[pid] = {"id": pid, "pair": rec.get("pair"), "seat": rec.get("seat"),
                        "detail": rec.get("detail"), "beat": rec.get("beat"),
                        "opened_ts": rec.get("ts"), "outcome": None,
                        "latency_s": None, "closed_ts": None}
        elif pid not in out:
            raise PairError(
                "orphan_close", f"pair id {pid!r} closed before it was opened",
                pair_id=pid)
        elif out[pid]["pair"] != rec.get("pair"):
            raise PairError(
                "pair_kind_mismatch",
                f"pair id {pid!r} opened as {out[pid]['pair']!r} but closed "
                f"as {rec.get('pair')!r}", pair_id=pid)
        elif out[pid]["outcome"] is not None:
            raise PairError(
                "pair_already_closed", f"pair id {pid!r} closed more than once",
                pair_id=pid)
        else:
            out[pid].update(outcome=rec.get("outcome"),
                            latency_s=rec.get("latency_s"),
                            closed_ts=rec.get("ts"),
                            close_detail=rec.get("detail"))
    return out


def open_now(ledger, kind=None):
    return [p for p in pairs(ledger).values()
            if p["outcome"] is None and (kind is None or p["pair"] == kind)]


def close_one(ledger, kinds, seat, outcomes, correlation_id=None, detail=None,
              ts=None, source=None, source_id=None, evidence=None):
    """Close exactly one compatible obligation, or fail closed.

    ``correlation_id`` selects one explicit ID.  Without it, resolution is
    allowed only when exactly one open obligation matches ``seat`` and one of
    ``kinds``.  Zero matches is a no-op; multiple matches are a typed
    ambiguity.  This is the safe local bridge until PORT-002 defines canonical
    correlation and PORT-003 supplies host-owned identity.
    """
    allowed_kinds = tuple(kinds)
    if not allowed_kinds or any(kind not in PAIR_KINDS for kind in allowed_kinds):
        raise PairError("unknown_pair_kind", "close_one received an invalid kind")
    if set(outcomes) != set(allowed_kinds):
        raise PairError(
            "invalid_resolution_map", "every compatible kind needs one outcome")
    state = pairs(ledger)
    if correlation_id is not None:
        if not isinstance(correlation_id, str) or not correlation_id.strip():
            raise PairError(
                "invalid_correlation", "correlation id must be a non-empty string")
        candidate = state.get(correlation_id)
        if candidate is None:
            raise PairError(
                "unknown_correlation",
                f"correlation {correlation_id!r} does not name an obligation",
                correlation_id=correlation_id)
        if candidate["outcome"] is not None:
            raise PairError(
                "pair_already_closed",
                f"correlation {correlation_id!r} names a closed obligation",
                correlation_id=correlation_id)
        if candidate["pair"] not in allowed_kinds:
            raise PairError(
                "incompatible_pair",
                f"correlation {correlation_id!r} names {candidate['pair']!r}, "
                f"not one of {', '.join(allowed_kinds)}",
                correlation_id=correlation_id, pair=candidate["pair"])
        if candidate.get("seat") != seat:
            raise PairError(
                "actor_mismatch",
                f"correlation {correlation_id!r} expects {candidate.get('seat')!r}, "
                f"not {seat!r}", correlation_id=correlation_id)
    else:
        candidates = [p for p in state.values()
                      if p["outcome"] is None and p["pair"] in allowed_kinds
                      and p.get("seat") == seat]
        if not candidates:
            return None
        if len(candidates) > 1:
            raise PairError(
                "ambiguous_correlation",
                f"{len(candidates)} compatible obligations are open for {seat!r}; "
                "supply an explicit pair id",
                pair_ids=[p["id"] for p in candidates])
        candidate = candidates[0]
    return close_pair(
        ledger, candidate["pair"], candidate["id"],
        outcomes[candidate["pair"]], detail=detail, ts=ts,
        opened_ts=candidate["opened_ts"], source=source, source_id=source_id,
        evidence=evidence)


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
