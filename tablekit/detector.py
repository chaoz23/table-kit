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

from . import engine as engine_mod
from . import pairs as pairs_mod


#: Checks that can only ever fire while the table is still sitting there.
#: `seat_quiet` is deliberately suppressed once the session ends (everyone is
#: quiet after a session, which is not a finding about anyone) — which means a
#: post-hoc sweep can never surface a starved seat. The report has to say so
#: rather than implying it looked.
LIVE_ONLY_CHECKS = ("seat_quiet", "unnarrated")

#: `ux.beat.text` is stored truncated. A beat at exactly this length is
#: known-incomplete evidence, so it cannot support an accusation.
_STORED_TEXT_LIMIT = 400


def _f(check, severity, detail, evidence=None, seat=None):
    return {"check": check, "severity": severity, "detail": detail,
            "evidence": evidence, "seat": seat}


def _gm_beats(rows):
    return [r for r in rows if r.get("type") == "ux.beat"]


def check(ledger, cfg=None, now=None, state=None):
    """Run every check. Returns a list of findings, most severe first.

    `state` is an optional dict from a game engine:
    `{"turn": seat_id, "log_len": int}`.
    Checks that need it are skipped when it is absent — skipped, not guessed.

    Narration progress is deliberately not trusted from `state`.  It advances
    only through `engine.acknowledge_narration()`, which correlates the
    acknowledgment to an exact durably ingested engine event.
    """
    now = now if now is not None else time.time()
    thr = (cfg.thresholds if cfg else {})
    rows = ledger.records()
    findings = []
    beats = _gm_beats(rows)
    last_beat = beats[-1] if beats else None

    # 1. Engine events the table was never told about. Requires the engine's
    #    own count — without it there is no way to know, so we do not guess.
    if state and state.get("log_len") is not None:
        narrated = engine_mod.narrated_through(ledger)
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
            if b.get("mention_ok"):
                # The sender already established this at send time, on the
                # untruncated text. Trust it over a re-scan of a stored copy
                # that may have lost a trailing mention to the 400-char cut.
                continue
            btext = b.get("text") or ""
            if len(btext) >= _STORED_TEXT_LIMIT:
                # The stored text is at the truncation limit, so it is KNOWN
                # INCOMPLETE — a trailing mention may simply have been cut off.
                # Accusing on evidence we know is partial is precisely the
                # no-false-accusation rule this module opens with: ambiguity
                # produces silence, not a finding. (Beats written by 0.5.0+
                # carry `mention_ok` and never reach this branch; this protects
                # sessions recorded before that.)
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
    #
    #    Only while the table is still running. If the GM's own last beat is
    #    older than the threshold, the session is over — and everyone being
    #    quiet after the session ends is not a finding about anyone. Without
    #    this, running the report the morning after accuses every seat at the
    #    table, which is exactly the cry-wolf failure that gets a checker
    #    ignored.
    session_live = bool(last_beat) and (now - last_beat.get("ts", now)) <= thr.get(
        "seat_quiet_s", 600)
    if cfg and session_live:
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
    """Write findings into the session file so the report can see them later.

    Always emits `qc.run` first. That event is the ONLY evidence that the
    checks were actually run, and it exists because the first version of this
    inferred "someone checked" from the presence of any `qc.finding` — which
    ingest also emits for `roll_result_advisory`. A session where nobody ran
    qc therefore looked checked, and the report went back to claiming clean.
    Deriving "was this examined?" from a side effect of something else is the
    same false-negative one level down.
    """
    out = [ledger.append("qc.run", findings=len(findings))]
    if not findings:
        return out + [ledger.append("qc.pass", checks=6)]
    return out + [ledger.append("qc.finding", check=f["check"], detail=f["detail"],
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
