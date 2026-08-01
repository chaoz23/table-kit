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

import hashlib
import json
import secrets
import time

from . import engine as engine_mod
from . import pairs as pairs_mod
from .events import SchemaError, make


#: Checks that can only ever fire while the table is still sitting there.
#: `seat_quiet` is deliberately suppressed once the session ends (everyone is
#: quiet after a session, which is not a finding about anyone) — which means a
#: post-hoc sweep can never surface a starved seat. The report has to say so
#: rather than implying it looked.
LIVE_ONLY_CHECKS = ("seat_quiet", "unnarrated")

#: `ux.beat.text` is stored truncated. A beat at exactly this length is
#: known-incomplete evidence, so it cannot support an accusation.
_STORED_TEXT_LIMIT = 400

EVALUATOR_VERSION = "tablekit-qc/1"
CHECK_IDS = (
    "unnarrated",
    "undeliverable_cue",
    "unanswered",
    "seat_quiet",
    "roll_unconsumed",
    "cue_unanswered",
    "long_beat",
    "split_beat",
)


def _digest(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _finding_id(check, seat=None, correlation=None):
    key = {"check": check, "seat": seat, "correlation": correlation}
    return f"{check}:{_digest(key)[:20]}"


def _f(check, severity, detail, evidence=None, seat=None, correlation=None):
    return {"check": check, "severity": severity, "detail": detail,
            "evidence": evidence, "seat": seat,
            "finding_id": _finding_id(check, seat, correlation)}


def _gm_beats(rows):
    return [r for r in rows if r.get("type") == "ux.beat"]


def check(ledger, cfg=None, now=None, state=None, enabled=None):
    """Run every check. Returns a list of findings, most severe first.

    `state` is an optional dict from a game engine:
    `{"turn": seat_id, "log_len": int}`.
    Checks that need it are skipped when it is absent — skipped, not guessed.

    Narration progress is deliberately not trusted from `state`.  It advances
    only through `engine.acknowledge_narration()`, which correlates the
    acknowledgment to an exact durably ingested engine event.
    """
    now = now if now is not None else time.time()
    state = _validate_state(state)
    enabled = set(CHECK_IDS if enabled is None else enabled)
    unknown = enabled - set(CHECK_IDS)
    if unknown:
        raise ValueError(f"unknown QC evaluator(s): {', '.join(sorted(unknown))}")
    thr = (cfg.thresholds if cfg else {})
    rows = ledger.records()
    findings = []
    beats = _gm_beats(rows)
    last_beat = beats[-1] if beats else None

    # 1. Engine events the table was never told about. Requires the engine's
    #    own count — without it there is no way to know, so we do not guess.
    if "unnarrated" in enabled and state and state.get("log_len") is not None:
        narrated = engine_mod.narrated_through(ledger)
        gap = state["log_len"] - narrated
        if gap > 0:
            findings.append(_f(
                "unnarrated", "defect",
                f"{gap} engine event(s) have happened that the table has not "
                "been told about",
                evidence=f"engine log at {state['log_len']}, narrated through {narrated}",
                correlation=f"engine:{narrated + 1}"))

    # 2. A cue that cannot be delivered. The one defect with no symptom at run
    #    time, so it is checked at the only moment it is still fixable.
    #
    #    Every cued beat is checked, not just the most recent one. An
    #    undeliverable cue from twenty minutes ago has not stopped being a
    #    problem — it is usually the *reason* a seat has gone quiet, and
    #    reporting the silence without the cause sends the GM looking in the
    #    wrong place.
    if "undeliverable_cue" in enabled and cfg:
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
                    seat=seat.id if seat else b["cued_seat"],
                    correlation=b.get("source_event_id") or f"beat:{i}"))

    # 3. Table spoke, GM has not. Only fires with positive evidence of an
    #    inbound message *after* the last beat — silence alone proves nothing,
    #    because a GM deliberately yielding the floor looks identical.
    if "unanswered" in enabled and last_beat:
        after = [r for r in rows if r.get("type") == "qa.inbound"
                 and r.get("ts", 0) > last_beat.get("ts", 0)]
        if after:
            who = sorted({r.get("seat") for r in after if r.get("seat")})
            findings.append(_f(
                "unanswered", "attention",
                f"{len(after)} table message(s) since your last beat",
                evidence=", ".join(who) or None,
                correlation=(last_beat.get("source_event_id")
                             or f"beat:{len(beats)}")))

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
    if "seat_quiet" in enabled and cfg and session_live:
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
                    seat=seat.id, correlation=f"last-seen:{last_seen}"))

    # 5. Called rolls nobody consumed, and cues nobody answered.
    for p in pairs_mod.open_now(ledger):
        ttl = thr.get(f"{p['pair']}_ttl_s")
        if not ttl or p["opened_ts"] is None or now - p["opened_ts"] <= ttl:
            continue
        if p["pair"] == "roll" and "roll_unconsumed" in enabled:
            findings.append(_f(
                "roll_unconsumed", "defect",
                f"a roll was called for {int((now - p['opened_ts']) // 60)} "
                "minutes ago and no outcome has been narrated",
                evidence=p.get("detail"), seat=p.get("seat"),
                correlation=p.get("id")))
        elif p["pair"] == "cue" and "cue_unanswered" in enabled:
            findings.append(_f(
                "cue_unanswered", "attention",
                f"{p.get('seat') or 'a seat'} was cued "
                f"{int((now - p['opened_ts']) // 60)} minutes ago and has not acted",
                evidence=p.get("detail"), seat=p.get("seat"),
                correlation=p.get("id")))

    # 6. Dosage: beat length and chunking. Advisory by construction.
    if last_beat:
        words = last_beat.get("words", 0)
        beat_correlation = (last_beat.get("source_event_id")
                            or f"beat:{len(beats)}")
        if ("long_beat" in enabled
                and words > thr.get("long_beat_words", 120)):
            findings.append(_f(
                "long_beat", "attention",
                f"that beat ran {words} words — long beats should be paying "
                "something off (a die landed, a new place, a silence to fill)",
                evidence=f"{words} words", correlation=beat_correlation))
        if ("split_beat" in enabled
                and last_beat.get("chunks", 1) > thr.get("max_chunks", 2)):
            findings.append(_f(
                "split_beat", "attention",
                f"that beat went out as {last_beat['chunks']} messages — the "
                "table will read them as separate beats and answer the first",
                evidence=f"{last_beat['chunks']} chunks",
                correlation=beat_correlation))

    order = {"defect": 0, "attention": 1}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    return findings


def _is_evaluation_output(rec):
    if rec.get("type") in ("qc.run", "qc.pass"):
        return True
    return rec.get("type") == "qc.finding" and bool(rec.get("evaluation_id"))

def input_snapshot(ledger):
    """Return a stable digest/cursor for evidence, excluding QC's own output."""
    rows = [r for r in ledger.read() if not _is_evaluation_output(r)]
    digest = _digest(rows)
    malformed = [r for r in rows if r.get("type") == "_malformed"]
    return {
        "count": len(rows),
        "digest": digest,
        "checked_through": f"ledger-input:{len(rows)}:sha256:{digest}",
        "malformed": malformed,
    }


def config_digest(cfg):
    if cfg is None:
        return None
    effective = {
        "name": cfg.name,
        "seats": [seat.as_dict() for seat in cfg.seats],
        "gm": cfg.gm.as_dict() if cfg.gm else None,
        "transport": cfg.transport,
        "thresholds": cfg.thresholds,
        "data_dir": cfg.data_dir,
    }
    return _digest(effective)


def _validate_state(state):
    if state is None:
        return None
    if not isinstance(state, dict):
        raise ValueError("engine state must be a JSON object")
    for field in ("log_len", "narrated_through"):
        value = state.get(field)
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, int) or value < 0):
            raise ValueError(f"engine state {field!r} must be a non-negative integer")
    if state.get("log_len") is not None and state.get("narrated_through") is not None:
        if state["narrated_through"] > state["log_len"]:
            raise ValueError("engine state narrated_through cannot exceed log_len")
    return state


def _eligibility(ledger, cfg, state):
    beats = ledger.beats()
    open_pairs = pairs_mod.open_now(ledger)
    return {
        "unnarrated": 1 if state and state.get("log_len") is not None else 0,
        "undeliverable_cue": sum(1 for b in beats if b.get("cued_seat")),
        "unanswered": 1 if beats else 0,
        "seat_quiet": len(cfg.player_seats) if cfg else 0,
        "roll_unconsumed": sum(1 for p in open_pairs if p["pair"] == "roll"),
        "cue_unanswered": sum(1 for p in open_pairs if p["pair"] == "cue"),
        "long_beat": 1 if beats else 0,
        "split_beat": 1 if beats else 0,
    }


def evaluate(ledger, cfg=None, now=None, state=None, enabled=None, check_fn=None):
    """Evaluate one immutable evidence snapshot and return a typed local result.

    This is the repo-local containment contract, not the suite-wide
    ``EvaluationResult v1`` RFC. Authority is explicitly self-attested.
    """
    when = now if now is not None else time.time()
    requested = list(CHECK_IDS if enabled is None else enabled)
    unknown = sorted(set(requested) - set(CHECK_IDS))
    snapshot = input_snapshot(ledger)
    errors = []
    skipped = []
    not_applicable = []
    disabled = [check_id for check_id in CHECK_IDS if check_id not in requested]
    active = set(requested) - set(unknown)

    if unknown:
        errors.append({"code": "unknown_evaluator", "evaluators": unknown})
    try:
        state = _validate_state(state)
    except (TypeError, ValueError) as e:
        errors.append({"code": "invalid_engine_state", "detail": str(e)})
        state = None
        if "unnarrated" in active:
            active.remove("unnarrated")
            skipped.append({"evaluator": "unnarrated", "reason": "invalid_engine_state"})

    if "unnarrated" in active and state is None:
        active.remove("unnarrated")
        not_applicable.append({"evaluator": "unnarrated",
                               "reason": "no_engine_state_supplied"})
    if state is not None and state.get("log_len") is None and "unnarrated" in active:
        active.remove("unnarrated")
        skipped.append({"evaluator": "unnarrated", "reason": "missing_log_len"})

    if cfg is None:
        for check_id in ("undeliverable_cue", "seat_quiet"):
            if check_id in active:
                active.remove(check_id)
                skipped.append({"evaluator": check_id, "reason": "missing_table_config"})

    beats = ledger.beats()
    if "seat_quiet" in active:
        quiet_s = cfg.thresholds.get("seat_quiet_s", 600)
        if not beats or when - beats[-1].get("ts", when) > quiet_s:
            active.remove("seat_quiet")
            skipped.append({"evaluator": "seat_quiet",
                            "reason": "live_window_not_observable"})

    if len(beats) == 0:
        errors.append({"code": "zero_eligible_beats",
                       "detail": "no GM beats were available to evaluate"})
    if snapshot["malformed"]:
        errors.extend({"code": "invalid_ledger_row", "line": row.get("line"),
                       "detail": row.get("error")} for row in snapshot["malformed"])
    if disabled:
        errors.append({"code": "disabled_evaluators", "evaluators": disabled})

    findings = []
    if active:
        try:
            runner = check if check_fn is None else check_fn
            findings = runner(ledger, cfg, now=when, state=state, enabled=active)
        except Exception as e:  # noqa: BLE001 - becomes typed incomplete evidence
            errors.append({"code": "evaluator_error", "detail":
                           f"{type(e).__name__}: {e}",
                           "evaluators": sorted(active)})
            skipped.extend({"evaluator": check_id, "reason": "evaluator_error"}
                           for check_id in sorted(active))
            active.clear()

    statuses = {}
    for check_id in CHECK_IDS:
        if check_id in disabled:
            statuses[check_id] = "disabled"
        elif any(s["evaluator"] == check_id for s in skipped):
            statuses[check_id] = "skipped"
        elif any(s["evaluator"] == check_id for s in not_applicable):
            statuses[check_id] = "not_applicable"
        elif check_id in active:
            statuses[check_id] = "evaluated"
        else:
            statuses[check_id] = "not_requested"

    incomplete = bool(errors or skipped)
    if incomplete:
        status = "incomplete"
    elif any(f.get("severity") == "defect" for f in findings):
        status = "findings"
    elif findings:
        status = "checked_with_advisories"
    else:
        status = "checked_clean"
    eligible = _eligibility(ledger, cfg, state)
    compatible = snapshot["count"] - len(snapshot["malformed"])
    evaluated_eligible = sum(eligible[check_id] for check_id in active)
    return {
        "schema": "tablekit.qc-result/1",
        "evaluation_id": f"qc-{int(when * 1000)}-{secrets.token_hex(5)}",
        "evaluator_version": EVALUATOR_VERSION,
        "authority_status": "self_attested",
        "status": status,
        "evaluated_at": when,
        "input_count": snapshot["count"],
        "input_digest": snapshot["digest"],
        "checked_through": snapshot["checked_through"],
        "config_digest": config_digest(cfg),
        "state_digest": _digest(state) if state is not None else None,
        "coverage": {
            "evaluators": [{"id": check_id, "status": statuses[check_id],
                            "eligible": eligible[check_id]}
                           for check_id in CHECK_IDS],
            "evaluated": sorted(active),
            "skipped": skipped,
            "not_applicable": not_applicable,
            "disabled": disabled,
            "counts": {
                "input": snapshot["count"],
                "compatible": compatible,
                "eligible": sum(eligible.values()),
                "evaluated": evaluated_eligible,
                "skipped_evaluators": len(skipped),
                "error_count": len(errors),
            },
        },
        "errors": errors,
        "findings": findings,
    }


def _legacy_result(ledger, findings):
    """Contain callers that still pass a bare finding list.

    A list has no proof of which snapshot or evaluator set produced it. It is
    recorded for compatibility, but can never authorize a clean report.
    """
    snapshot = input_snapshot(ledger)
    return {
        "schema": "tablekit.qc-result/1",
        "evaluation_id": f"qc-{int(time.time() * 1000)}-{secrets.token_hex(5)}",
        "evaluator_version": EVALUATOR_VERSION,
        "authority_status": "self_attested",
        "status": "incomplete",
        "evaluated_at": time.time(),
        "input_count": snapshot["count"],
        "input_digest": snapshot["digest"],
        "checked_through": snapshot["checked_through"],
        "config_digest": None,
        "state_digest": None,
        "coverage": {"evaluators": [], "evaluated": [],
                     "skipped": [{"evaluator": "suite",
                                  "reason": "missing_evaluation_metadata"}],
                     "not_applicable": [], "disabled": list(CHECK_IDS)},
        "errors": [{"code": "missing_evaluation_metadata",
                    "detail": "record() received findings without evaluate() metadata"}],
        "findings": findings,
    }


def _latest_finding_states(ledger):
    states = {}
    observations = {}
    for rec in ledger.read(etype="qc.finding"):
        fid = rec.get("finding_id")
        if not fid or not rec.get("evaluation_id"):
            continue
        states[fid] = rec
        observations[fid] = observations.get(fid, 0) + (rec.get("status") == "open")
    return states, observations


def _evaluation_runs(ledger):
    return {rec.get("evaluation_id"): rec
            for rec in ledger.read(etype="qc.run") if rec.get("evaluation_id")}


def record(ledger, result_or_findings):
    """Durably record one evaluation and explicit finding transitions."""
    result = (result_or_findings if isinstance(result_or_findings, dict)
              else _legacy_result(ledger, result_or_findings))
    required = ("evaluation_id", "status", "input_digest", "checked_through",
                "coverage", "errors", "findings")
    missing = [key for key in required if key not in result]
    if missing:
        raise SchemaError(f"evaluation result missing: {', '.join(missing)}")
    current_snapshot = input_snapshot(ledger)
    if (result.get("input_digest") != current_snapshot["digest"]
            or result.get("input_count") != current_snapshot["count"]):
        raise SchemaError(
            "ledger input changed between evaluation and record; rerun QC on "
            "the current snapshot")

    current = []
    for finding in result["findings"]:
        finding = dict(finding)
        finding.setdefault("finding_id", _finding_id(
            finding.get("check", "unknown"), finding.get("seat"),
            finding.get("correlation") or finding.get("evidence")))
        current.append(finding)
    current_ids = {f["finding_id"] for f in current}
    if len(current_ids) != len(current):
        raise SchemaError("evaluation contains duplicate finding IDs")
    prior, _observations = _latest_finding_states(ledger)
    prior_runs = _evaluation_runs(ledger)
    evaluated = set(result["coverage"].get("evaluated", []))

    status = result["status"]
    has_defect = any(f.get("severity") == "defect" for f in current)
    if status == "checked_clean" and current:
        raise SchemaError("checked_clean evaluation cannot contain findings")
    if status == "checked_with_advisories" and (not current or has_defect):
        raise SchemaError(
            "checked_with_advisories requires attention findings and no defects")
    if status == "findings" and not has_defect:
        raise SchemaError("findings status requires at least one defect")
    if status in ("checked_clean", "checked_with_advisories", "findings"):
        if (result.get("errors") or result["coverage"].get("skipped")
                or result["coverage"].get("disabled")):
            raise SchemaError(
                f"{status} evaluation cannot contain errors, skips, or disabled checks")

    run_fields = {
        "findings": len(current),
        "evaluation_id": result["evaluation_id"],
        "result_schema": result.get("schema"),
        "status": result["status"],
        "authority_status": result.get("authority_status", "self_attested"),
        "evaluator_version": result.get("evaluator_version", EVALUATOR_VERSION),
        "evaluated_at": result.get("evaluated_at"),
        "input_count": result.get("input_count"),
        "input_digest": result["input_digest"],
        "checked_through": result["checked_through"],
        "config_digest": result.get("config_digest"),
        "state_digest": result.get("state_digest"),
        "coverage": result["coverage"],
        "errors": result["errors"],
        "finding_ids": sorted(current_ids),
    }
    planned = [("qc.run", run_fields)]
    for finding in current:
        planned.append(("qc.finding", {
            "check": finding["check"], "detail": finding["detail"],
            "severity": finding["severity"], "evidence": finding.get("evidence"),
            "seat": finding.get("seat"), "finding_id": finding["finding_id"],
            "status": "open", "evaluation_id": result["evaluation_id"],
        }))

    # Absence resolves only evaluators that actually ran. A skipped evaluator
    # cannot close its old findings merely by failing to look this time.
    transition_ids = []
    for fid, old in prior.items():
        if (old.get("status") == "open" and fid not in current_ids
                and old.get("check") in evaluated):
            old_run = prior_runs.get(old.get("evaluation_id"), {})
            transition = "resolved"
            if (old_run.get("evaluator_version") != result.get("evaluator_version")
                    or old_run.get("config_digest") != result.get("config_digest")):
                transition = "superseded"
            planned.append(("qc.finding", {
                "check": old["check"], "detail": old["detail"],
                "severity": old.get("severity", "defect"),
                "evidence": old.get("evidence"), "seat": old.get("seat"),
                "finding_id": fid, "status": transition,
                "evaluation_id": result["evaluation_id"],
            }))
            transition_ids.append(fid)

    run_fields["transition_ids"] = sorted(transition_ids)
    run_fields["expected_output_events"] = (
        len(current) + len(transition_ids) +
        (1 if result["status"] == "checked_clean" else 0))

    if result["status"] == "checked_clean":
        planned.append(("qc.pass", {"checks": len(evaluated),
                                    "evaluation_id": result["evaluation_id"]}))
    # Validate the whole logical write before the first durable mutation. A
    # process crash can still interrupt sequential appends; the report detects
    # that as incomplete rather than blessing a partial result.
    for etype, fields in planned:
        make(etype, **fields)
    out = [ledger.append(etype, **fields) for etype, fields in planned]
    return out


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


def format_evaluation(result):
    lines = [format_findings(result["findings"]),
             f"QC status: {result['status']} ({result['authority_status']})",
             f"checked through: {result['checked_through']}"]
    for item in result["coverage"].get("skipped", []):
        lines.append(f"  skipped {item['evaluator']}: {item['reason']}")
    for error in result.get("errors", []):
        lines.append(f"  {error['code']}: {error.get('detail') or error.get('evaluators')}")
    return "\n".join(lines)


def exit_code(result):
    if result["status"] in ("incomplete", "invalid", "internal_error"):
        return 2
    if result["status"] in ("findings", "checked_with_advisories"):
        return 1
    return 0
