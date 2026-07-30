"""QC — checks that run against the session file, between beats.

Two severities, and the distinction is load-bearing:

  * **defect** — a boundary was crossed. Something was true or it was not: a
    cue to an agent seat that cannot be delivered, an engine event the table
    was never told about, a seat that has not been addressed in twenty
    minutes. These are pass/fail and they are worth interrupting for.
  * **attention** — a dosage reading. Beats running long, chunks running high.
    Real signal, no boundary crossed, and the right response is usually to
    note it and carry on.

Only defects are allowed to accuse. Every check here follows the rule the
conduct checker learned the hard way: **a finding requires positive evidence,
and ambiguity produces silence rather than an accusation.** A checker that
cries wolf during play is worse than no checker, because the GM stops reading
it in the first twenty minutes and then it is just noise with a maintenance
cost.

Everything runs off the session file. No live platform calls, so the same
checks work on a finished session, on someone else's session, and in a test.
"""

import time

from . import pairs as pairs_mod


def _f(check, severity, detail, evidence=None, seat=None):
    return {"check": check, "severity": severity, "detail": detail,
            "evidence": evidence, "seat": seat}


def _gm_beats(rows):
    return [r for r in rows if r.get("type") == "ux.beat"]


def check(ledger, cfg=None, now=None, state=None):
    """Run every check. Returns a list of findings, most severe first.

    `state` is an optional dict from a game engine:
    `{"turn": seat_id, "log_len": int, "narrated_through": int}`.
    Checks that need it are skipped when it is absent — skipped, not guessed.
    """
    now = now if now is not None else time.time()
    thr = (cfg.thresholds if cfg else {})
    rows = ledger.read()
    findings = []
    beats = _gm_beats(rows)
    last_beat = beats[-1] if beats else None

    # 1. Engine events the table was never told about. Requires the engine's
    #    own count — without it there is no way to know, so we do not guess.
    if state and state.get("log_len") is not None:
        narrated = state.get("narrated_through")
        if narrated is None:
            mark = ledger.last(etype="qc.mark")
            narrated = mark.get("narrated_through") if mark else 0
        gap = state["log_len"] - narrated
        if gap > 0:
            findings.append(_f(
                "unnarrated", "defect",
                f"{gap} engine event(s) have happened that the table has not "
                "been told about",
                evidence=f"engine log at {state['log_len']}, narrated through {narrated}"))

    # 2. A cue that cannot be delivered. The one defect with no symptom at run
    #    time, so it is checked at the only moment it is still fixable.
    #
    #    Every cued beat is checked, not just the most recent one. An
    #    undeliverable cue from twenty minutes ago has not stopped being a
    #    problem — it is usually the *reason* a seat has gone quiet, and
    #    reporting the silence without the cause sends the GM looking in the
    #    wrong place.
    if cfg:
        for i, b in enumerate(beats, 1):
            if not b.get("cued_seat"):
                continue
            seat = cfg.seat(b["cued_seat"])
            problem = cfg.mention_check(b.get("text", ""), seat)
            if problem:
                findings.append(_f(
                    "undeliverable_cue", "defect", f"beat {i}: {problem}",
                    evidence=(b.get("text", "") or "")[:160],
                    seat=seat.id if seat else b["cued_seat"]))

    # 3. Table spoke, GM has not. Only fires with positive evidence of an
    #    inbound message *after* the last beat — silence alone proves nothing,
    #    because a GM deliberately yielding the floor looks identical.
    if last_beat:
        after = [r for r in rows if r.get("type") == "qa.inbound"
                 and r.get("ts", 0) > last_beat.get("ts", 0)]
        if after:
            who = sorted({r.get("seat") for r in after if r.get("seat")})
            findings.append(_f(
                "unanswered", "attention",
                f"{len(after)} table message(s) since your last beat",
                evidence=", ".join(who) or None))

    # 4. A seat that has gone quiet and has not been checked on. Live-failure
    #    rule: agents will happily carry a scene to its climax while a human
    #    seat sits unaddressed, and nothing in the record looks wrong.
    if cfg:
        quiet_s = thr.get("seat_quiet_s", 600)
        inbound = [r for r in rows if r.get("type") == "qa.inbound"]
        session_start = rows[0]["ts"] if rows else now
        checked = {p["seat"] for p in pairs_mod.open_now(ledger, "checkin")}
        for seat in cfg.player_seats:
            last_seen = max([r["ts"] for r in inbound if r.get("seat") == seat.id],
                            default=session_start)
            idle = now - last_seen
            if idle > quiet_s and seat.id not in checked:
                findings.append(_f(
                    "seat_quiet", "defect",
                    f"{seat.display} has not said anything for "
                    f"{int(idle // 60)} minutes and has not been checked on",
                    evidence=f"idle {int(idle)}s > threshold {int(quiet_s)}s",
                    seat=seat.id))

    # 5. Called rolls nobody consumed, and cues nobody answered.
    for p in pairs_mod.open_now(ledger):
        ttl = thr.get(f"{p['pair']}_ttl_s")
        if not ttl or p["opened_ts"] is None or now - p["opened_ts"] <= ttl:
            continue
        if p["pair"] == "roll":
            findings.append(_f(
                "roll_unconsumed", "defect",
                f"a roll was called for {int((now - p['opened_ts']) // 60)} "
                "minutes ago and no outcome has been narrated",
                evidence=p.get("detail"), seat=p.get("seat")))
        elif p["pair"] == "cue":
            findings.append(_f(
                "cue_unanswered", "attention",
                f"{p.get('seat') or 'a seat'} was cued "
                f"{int((now - p['opened_ts']) // 60)} minutes ago and has not acted",
                evidence=p.get("detail"), seat=p.get("seat")))

    # 6. Dosage: beat length and chunking. Advisory by construction.
    if last_beat:
        words = last_beat.get("words", 0)
        if words > thr.get("long_beat_words", 120):
            findings.append(_f(
                "long_beat", "attention",
                f"that beat ran {words} words — long beats should be paying "
                "something off (a die landed, a new place, a silence to fill)",
                evidence=f"{words} words"))
        if last_beat.get("chunks", 1) > thr.get("max_chunks", 2):
            findings.append(_f(
                "split_beat", "attention",
                f"that beat went out as {last_beat['chunks']} messages — the "
                "table will read them as separate beats and answer the first",
                evidence=f"{last_beat['chunks']} chunks"))

    order = {"defect": 0, "attention": 1}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    return findings


def record(ledger, findings):
    """Write findings into the session file so the report can see them later."""
    if not findings:
        return [ledger.append("qc.pass", checks=6)]
    return [ledger.append("qc.finding", check=f["check"], detail=f["detail"],
                          severity=f["severity"], evidence=f.get("evidence"),
                          seat=f.get("seat")) for f in findings]


def format_findings(findings):
    if not findings:
        return "QC: pass"
    lines = []
    for f in findings:
        tag = "DEFECT " if f["severity"] == "defect" else "attention"
        lines.append(f"  [{tag}] {f['check']}: {f['detail']}")
        if f.get("evidence"):
            lines.append(f"            evidence: {f['evidence']}")
    defects = sum(1 for f in findings if f["severity"] == "defect")
    head = (f"QC: {defects} defect(s), {len(findings) - defects} attention item(s)"
            if defects else f"QC: {len(findings)} attention item(s), no defects")
    return head + "\n" + "\n".join(lines)
