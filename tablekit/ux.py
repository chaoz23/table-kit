"""UX — what the evening looked like from each seat, derived from the record.

Everything here is arithmetic on the session file: who spoke, how often, how
long they waited, how long they went unaddressed. No judgment and no scoring.

That restraint is deliberate. Seat airtime is the most tempting number in this
file to turn into a grade, and it is a bad grade: a player can have a superb
evening in four lines, and a table where everyone speaks equally can be one
where nobody is doing anything. What these numbers are actually good for is
noticing *shapes* — a seat whose longest gap is forty minutes, a session where
the GM holds eighty percent of the words — and then going and looking at what
was happening there.

The one place this lane does draw a line is idle time, because a seat that has
not been addressed in twenty minutes is a fact about the table and not a
matter of taste. That check lives in `detector` as a defect; here it is just
reported.
"""


def _words(t):
    return len((t or "").split())


def session_span(rows):
    ts = [r["ts"] for r in rows if r.get("ts")]
    if not ts:
        return None, None, 0.0
    return min(ts), max(ts), max(ts) - min(ts)


def seat_stats(ledger, cfg=None, now=None):
    """Per-seat participation and waiting.

    Silence is measured against the last thing in the record, not against the
    clock, so a report reads the same tomorrow as it did at midnight. Live
    callers who want "how long has this seat been quiet *right now*" pass
    `now` — and the detector, which is the thing that accuses, always does.
    """
    rows = ledger.records()
    start, end, _ = session_span(rows)
    if start is None:
        return {}
    if now is not None:
        end = max(end, now)
    # Receipts are immutable. If a previously unknown source ID is repaired by
    # a later exact config mapping, the routed disposition carries the repaired
    # seat; derive metrics from that fact without rewriting history.
    repaired_seats = {}
    for route in rows:
        key = (route.get("source"), route.get("source_id"))
        if (route.get("type") == "qa.route" and all(key)
                and route.get("seat") not in (None, "unknown")
                and route.get("status") != "quarantined"):
            repaired_seats[key] = route["seat"]
    inbound = []
    for rec in rows:
        if rec.get("type") != "qa.inbound":
            continue
        key = (rec.get("source"), rec.get("source_id"))
        effective = repaired_seats.get(key)
        inbound.append({**rec, "seat": effective or rec.get("seat")})
    turns = [r for r in rows if r.get("type") == "ux.turn"]
    signals = [r for r in rows if r.get("type") == "uxr.signal"]
    cues = [r for r in rows if r.get("type") == "out.open" and r.get("pair") == "cue"]

    seats = ([s.id for s in cfg.player_seats] if cfg
             else sorted({r.get("seat") for r in inbound if r.get("seat")}))
    out = {}
    for sid in seats:
        mine = [r for r in inbound if r.get("seat") == sid]
        stamps = [start] + [r["ts"] for r in mine] + [end]
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        waits = sorted(r.get("wait_s", 0) for r in turns if r.get("seat") == sid)
        mk = {}
        for m in signals:
            if m.get("seat") == sid:
                mk[m["signal"]] = mk.get(m["signal"], 0) + 1
        out[sid] = {
            "lines": len(mine),
            "words": sum(_f_words(r) for r in mine),
            "cued": sum(1 for c in cues if c.get("seat") == sid),
            "longest_silence_s": round(max(gaps), 1) if gaps else 0.0,
            "median_wait_s": waits[len(waits) // 2] if waits else None,
            "signals": mk or None,
        }
    total_words = sum(v["words"] for v in out.values())
    gm_words = sum(r.get("words", 0) for r in rows if r.get("type") == "ux.beat")
    table_words = total_words + gm_words
    for v in out.values():
        v["share_of_player_words"] = (round(v["words"] / total_words, 3)
                                      if total_words else None)
    return {"seats": out, "gm_words": gm_words, "player_words": total_words,
            "gm_share_of_table": round(gm_words / table_words, 3) if table_words else None}


def _f_words(rec):
    """Inbound events carry a char count, not text — the kit does not store
    player prose it was not asked to keep. Words are estimated from characters
    at the usual ~5.5 chars/word, and the estimate is labelled as one wherever
    it surfaces."""
    if rec.get("words") is not None:
        return rec["words"]
    return int(round((rec.get("chars", 0) or 0) / 5.5))


def beat_stats(ledger):
    beats = ledger.beats()
    if not beats:
        return {"beats": 0}
    words = sorted(b.get("words", 0) for b in beats)
    chunks = [b.get("chunks", 1) for b in beats]
    start, end, span = session_span(ledger.records())
    return {
        "beats": len(beats),
        "median_words": words[len(words) // 2],
        "p90_words": words[int(len(words) * 0.9)] if words else 0,
        "longest_words": words[-1],
        "split_beats": sum(1 for c in chunks if c > 1),
        "span_s": round(span, 1),
        "beats_per_hour": round(len(beats) / (span / 3600), 1) if span > 60 else None,
    }


def transport_stats(ledger):
    """QA lane: did the plumbing hold up?"""
    rows = ledger.read(lane="qa")
    posts = [r for r in rows if r["type"] == "qa.post"]
    failed = [r for r in rows if r["type"] == "qa.post_failed"]
    lat = sorted(r["latency_ms"] for r in posts if r.get("latency_ms") is not None)
    listener = [r for r in rows if r["type"] == "qa.listener"]
    drops = [r for r in listener if r.get("state") in ("down", "reconnect")]
    cmds = [r for r in rows if r["type"] == "qa.command"]
    routes = [r for r in rows if r["type"] == "qa.route"]
    quarantine_events = [r for r in routes if r.get("status") == "quarantined"]
    latest_routes = {}
    for index, route in enumerate(routes):
        key = ((route.get("source"), route.get("source_id"))
               if route.get("source_id") else ("manual-row", index))
        latest_routes[key] = route
    quarantines = [r for r in latest_routes.values()
                   if r.get("status") == "quarantined"]
    quarantine_reasons = {}
    for route in quarantines:
        reason = route.get("reason") or "unknown"
        quarantine_reasons[reason] = quarantine_reasons.get(reason, 0) + 1
    rolls = [r for r in ledger.read(etype="act") if r.get("roll_total") is not None]
    by_route = {}
    for r in rolls:
        by_route[r.get("via") or "dm"] = by_route.get(r.get("via") or "dm", 0) + 1
    return {
        "rolls_by_route": by_route or None,
        "posts": len(posts),
        "post_failures": len(failed),
        "median_post_latency_ms": lat[len(lat) // 2] if lat else None,
        "listener_interruptions": len(drops),
        "commands": len(cmds),
        "command_failures": sum(1 for c in cmds if not c.get("ok")),
        "routing_quarantines": len(quarantines),
        "routing_quarantine_events": len(quarantine_events),
        "routing_quarantine_reasons": quarantine_reasons or None,
        "routing_advisories": sum(1 for r in routes
                                  if r.get("status") == "advisory"),
        "malformed_records": len([r for r in ledger.read()
                                  if r.get("type") == "_malformed"]),
    }
