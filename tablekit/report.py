"""The post-session report — five lanes, no score.

There is no composite number at the bottom of this report and there is not
going to be one. A single "table quality: 78" would be read, believed, and
optimised against, and it would be a fiction assembled from a handful of
inferred signals, some latencies, and a couple of boundary checks. The
report's job is to put the evening's evidence where a human can look at it,
sorted so the parts that are actually load-bearing come first.

What it *will* say plainly:

  * every categorical defect, because those are pass/fail
  * every signal inferred from what a player said, anchored to the beat it
    was about and carrying their own words as evidence
  * the user stories those signals imply, in the seat's own framing
  * what people actually said when asked at the close
  * outcome-pair results, with rates withheld below a floor

Exit codes follow the family convention: 0 clean, 1 findings to look at,
2 refused — not enough happened to say anything, which is a different claim
from "the session was fine".
"""

import json

from . import detector as detector_mod
from . import pairs as pairs_mod
from . import ux as ux_mod
from . import uxr as uxr_mod

#: A session shorter than this has nothing to report on, and saying "no
#: defects found" about it would be a lie of omission.
MIN_BEATS = 3


def build(ledger, cfg=None, min_pattern=uxr_mod.MIN_PATTERN):
    rows = ledger.records()
    audit_rows = ledger.read()
    beats = ledger.beats()
    signals = [r for r in rows if r.get("type") == "uxr.signal"]
    debriefs = [r for r in rows if r.get("type") == "uxr.debrief"]
    recorded_findings = [r for r in rows if r.get("type") == "qc.finding"]
    beat_text = {i + 1: (b.get("text") or "")[:160] for i, b in enumerate(beats)}

    by_signal = {}
    for m in signals:
        by_signal.setdefault(m["signal"], []).append(m)

    #: Below the floor, a signal is a moment and not a tendency. The report
    #: says which it is holding, every time, rather than leaving the reader to
    #: guess how much weight the number carries.
    patterns, moments = [], []
    for name, group in sorted(by_signal.items(), key=lambda kv: -len(kv[1])):
        info = uxr_mod.SIGNALS[name]
        entry = {"signal": name, "means": info["means"], "count": len(group),
                 "positive": name in uxr_mod.POSITIVE,
                 "seats": sorted({g.get("seat") for g in group if g.get("seat")}),
                 "beats": [g.get("beat") for g in group],
                 "sources": sorted({g.get("source", "dm") for g in group})}
        (patterns if len(group) >= min_pattern else moments).append(entry)

    runs = [r for r in rows if r.get("type") == "qc.run"
            and r.get("evaluation_id")]
    latest_run = runs[-1] if runs else None
    snapshot = detector_mod.input_snapshot(ledger)
    coverage_errors = []
    if latest_run is None:
        coverage_errors.append({"code": "not_checked",
                                "detail": "no typed QC evaluation is recorded"})
    else:
        complete_statuses = {"checked_clean", "checked_with_advisories", "findings"}
        if latest_run.get("status") not in complete_statuses:
            coverage_errors.extend(latest_run.get("errors") or [
                {"code": "recorded_run_incomplete",
                 "detail": "the latest QC run did not evaluate complete evidence"}
            ])
        elif latest_run.get("errors"):
            coverage_errors.append({
                "code": "inconsistent_qc_run",
                "detail": "a complete status contains recorded evaluator errors",
            })
        if latest_run.get("input_digest") != snapshot["digest"]:
            coverage_errors.append({
                "code": "stale_qc",
                "detail": "ledger input changed after the latest QC run",
                "checked_through": latest_run.get("checked_through"),
                "current": snapshot["checked_through"],
            })
        if latest_run.get("input_count") != snapshot["count"]:
            coverage_errors.append({
                "code": "input_count_mismatch",
                "detail": (f"recorded {latest_run.get('input_count')}; "
                           f"current {snapshot['count']}"),
            })
        if latest_run.get("evaluator_version") != detector_mod.EVALUATOR_VERSION:
            coverage_errors.append({
                "code": "evaluator_version_drift",
                "detail": (f"recorded {latest_run.get('evaluator_version')!r}; "
                           f"current {detector_mod.EVALUATOR_VERSION!r}"),
            })
        current_config_digest = detector_mod.config_digest(cfg)
        if latest_run.get("config_digest") != current_config_digest:
            coverage_errors.append({
                "code": "config_drift",
                "detail": "current table configuration differs from the checked run",
            })
        evaluator_rows = (latest_run.get("coverage") or {}).get("evaluators", [])
        incomplete_evaluators = [
            item.get("id") for item in evaluator_rows
            if item.get("status") in ("skipped", "disabled", "not_requested")
        ]
        if incomplete_evaluators:
            coverage_errors.append({
                "code": "incomplete_evaluator_coverage",
                "evaluators": incomplete_evaluators,
            })
        evaluation_id = latest_run.get("evaluation_id")
        run_findings = [r for r in recorded_findings
                        if r.get("evaluation_id") == evaluation_id]
        open_ids = sorted(r.get("finding_id") for r in run_findings
                          if r.get("status") == "open")
        transition_ids = sorted(r.get("finding_id") for r in run_findings
                                if r.get("status") in ("resolved", "superseded"))
        pass_seen = any(r.get("type") == "qc.pass"
                        and r.get("evaluation_id") == evaluation_id for r in rows)
        expected_open = sorted(latest_run.get("finding_ids") or [])
        expected_transitions = sorted(latest_run.get("transition_ids") or [])
        expected_outputs = latest_run.get("expected_output_events")
        actual_outputs = len(run_findings) + (1 if pass_seen else 0)
        if (open_ids != expected_open
                or transition_ids != expected_transitions
                or (latest_run.get("status") == "checked_clean" and not pass_seen)
                or (latest_run.get("status") != "checked_clean" and pass_seen)
                or expected_outputs != actual_outputs):
            coverage_errors.append({
                "code": "incomplete_qc_commit",
                "detail": (f"expected {expected_outputs} result event(s), found "
                           f"{actual_outputs}; rerun QC"),
            })
    malformed = [r for r in audit_rows if r.get("type") == "_malformed"]
    if malformed and not any(e.get("code") == "invalid_ledger_row"
                             for e in coverage_errors):
        coverage_errors.extend({"code": "invalid_ledger_row",
                                "line": row.get("line"),
                                "detail": row.get("error")}
                               for row in malformed)

    # Sweep now regardless, so there is always a verdict rather than only
    # whatever happened to be recorded. Post-hoc findings are labelled: they
    # are a weaker form of evidence than a check run while the table sat there.
    posthoc = []
    posthoc_errors = []
    try:
        for pf in detector_mod.check(ledger, cfg):
            # Live-only checks are excluded on purpose. `seat_quiet` can still
            # fire here if the last beat happens to be recent, but reporting it
            # would contradict the very message printed above — that these
            # checks were never given the chance. A sweep that claims to have
            # done the thing it says it could not do is worse than one that
            # admits the gap.
            if pf.get("check") in detector_mod.LIVE_ONLY_CHECKS:
                continue
            pf = dict(pf)
            pf["found"] = "post-hoc"
            posthoc.append(pf)
    except Exception as e:  # noqa: BLE001 - surface, never silently bless
        posthoc = []
        posthoc_errors.append({"code": "posthoc_evaluator_error",
                               "detail": f"{type(e).__name__}: {e}"})
        coverage_errors.extend(posthoc_errors)

    # Reconstruct lifecycle from explicit status transitions. Detail is
    # mutable evidence text and never part of identity.
    states, observations = {}, {}
    legacy = []
    for finding in recorded_findings:
        fid = finding.get("finding_id")
        if fid and finding.get("evaluation_id"):
            states[fid] = finding
            if finding.get("status") == "open":
                observations[fid] = observations.get(fid, 0) + 1
        else:
            legacy.append(finding)

    current, historical = [], []
    for fid, finding in states.items():
        entry = {"finding_id": fid, "check": finding["check"],
                 "detail": finding.get("detail"), "seat": finding.get("seat"),
                 "evidence": finding.get("evidence"),
                 "severity": finding.get("severity", "defect"),
                 "status": finding.get("status"), "found": "live",
                 "seen": observations.get(fid, 0)}
        (current if finding.get("status") == "open" else historical).append(entry)

    # Findings emitted by ingest/older versions have no lifecycle metadata.
    # They remain visible as observed evidence but cannot prove QC coverage.
    legacy_seen = {}
    for finding in legacy:
        key = (finding.get("check"), finding.get("seat"), finding.get("detail"))
        if key in legacy_seen:
            legacy_seen[key]["seen"] += 1
            continue
        entry = {"finding_id": None, "check": finding["check"],
                 "detail": finding.get("detail"), "seat": finding.get("seat"),
                 "evidence": finding.get("evidence"), "status": "observed",
                 "severity": finding.get("severity", "defect"),
                 "found": "live", "seen": 1}
        legacy_seen[key] = entry
        current.append(entry)

    current_ids = {f.get("finding_id") for f in current if f.get("finding_id")}
    unrecorded_posthoc = []
    for finding in posthoc:
        fid = finding.get("finding_id")
        if fid and fid in current_ids:
            continue
        entry = dict(finding)
        entry.update(status="observed", found="post-hoc", seen=1)
        current.append(entry)
        unrecorded_posthoc.append(fid or finding.get("check"))
    if unrecorded_posthoc:
        coverage_errors.append({"code": "posthoc_unrecorded_findings",
                                "detail": "report-time evaluation found evidence "
                                          "not present in the latest recorded run",
                                "findings": unrecorded_posthoc})

    defects = [f for f in current if f.get("severity") == "defect"]
    attention = [f for f in current if f.get("severity") != "defect"]
    enough_data = len(beats) >= MIN_BEATS
    coverage_complete = bool(enough_data and latest_run and not coverage_errors)
    if not enough_data:
        coverage_errors.append({"code": "insufficient_sample",
                                "detail": f"{len(beats)} beats; floor is {MIN_BEATS}"})
        coverage_complete = False
    if not coverage_complete:
        qc_status = "incomplete"
    elif defects:
        qc_status = "findings"
    elif attention:
        qc_status = "checked_with_advisories"
    else:
        qc_status = "checked_clean"
    unchecked_ranges = []
    if latest_run is None:
        unchecked_ranges.append({"after": None,
                                 "through": snapshot["checked_through"]})
    elif latest_run.get("input_digest") != snapshot["digest"]:
        unchecked_ranges.append({"after": latest_run.get("checked_through"),
                                 "through": snapshot["checked_through"]})
    evaluator_inventory = ((latest_run.get("coverage") or {}).get(
        "evaluators", []) if latest_run else [])
    return {
        "table": cfg.name if cfg else None,
        "enough_data": enough_data,
        "beats": ux_mod.beat_stats(ledger),
        "seats": ux_mod.seat_stats(ledger, cfg),
        "transport": ux_mod.transport_stats(ledger),
        "qc": {
            "status": qc_status,
            "coverage_complete": coverage_complete,
            "authority_status": "self_attested",
            "ran_live": bool(latest_run),
            "latest_evaluation_id": (latest_run.get("evaluation_id")
                                     if latest_run else None),
            "checked_through": (latest_run.get("checked_through")
                                if latest_run else None),
            "current_cursor": snapshot["checked_through"],
            "unchecked_ranges": unchecked_ranges,
            "coverage_errors": coverage_errors,
            "evaluators": evaluator_inventory,
            "live_only_unchecked": [
                check_id for check_id in detector_mod.LIVE_ONLY_CHECKS
                if not latest_run or not any(
                    item.get("id") == check_id
                    and item.get("status") in ("evaluated", "not_applicable")
                    for item in evaluator_inventory)
            ],
            "defects": defects,
            "attention": attention,
            "historical_findings": historical,
        },
        "uxr": {
            "signals_total": len(signals),
            "patterns": patterns,
            "moments": moments,
            "floor": min_pattern,
            "stories": uxr_mod.stories(signals, beat_text),
            "debriefs": [{"seat": d.get("seat"), "question": d.get("question"),
                          "answer": d.get("answer"), "signal": d.get("signal")}
                         for d in debriefs],
            "asked": bool(debriefs),
        },
        "outcomes": pairs_mod.summary(ledger),
        "parked": [{"topic": r.get("topic"), "detail": r.get("detail"),
                    "seat": r.get("seat")}
                   for r in rows if r.get("type") == "qa.delta"],
    }


def _line(label, value, unit=""):
    return f"  {label:<28} {value}{unit}" if value is not None else \
           f"  {label:<28} —"


def render(rep):
    L = []
    add = L.append
    add(f"# Session report — {rep.get('table') or 'table'}")
    add("")

    if not rep["enough_data"]:
        add(f"Not enough happened to report on ({rep['beats'].get('beats', 0)} "
            f"beats, floor is {MIN_BEATS}).")
        add("This is a refusal of aggregate claims, but observed findings remain visible below.")
        add("")

    # --- 1. defects first: the only pass/fail content in the report -----
    d = rep["qc"]["defects"]
    if d:
        add("## Defects")
    elif not rep["qc"].get("coverage_complete"):
        label = ("NOT CHECKED DURING PLAY"
                 if not rep["qc"].get("ran_live") else "COVERAGE INCOMPLETE")
        add(f"## Defects — {label}")
        add("  This is an absence of authoritative checking, not an absence of defects.")
    else:
        add("## Defects — none")
    for f in d:
        persisted = (f" (still open across {f['seen']} checks)"
                     if f.get("seen", 1) > 1 else "")
        when = " [found post-hoc]" if f.get("found") == "post-hoc" else ""
        add(f"  ✗ {f['check']}: {f['detail']}{persisted}{when}")
        if f.get("evidence"):
            add(f"      evidence: {f['evidence']}")
    add("")
    add(f"QC evidence authority: {rep['qc'].get('authority_status', 'unknown')}")
    add("")

    if not rep["qc"].get("coverage_complete"):
        add("## QC coverage — INCOMPLETE")
        add(f"  checked through: {rep['qc'].get('checked_through') or 'never'}")
        add(f"  current cursor:  {rep['qc'].get('current_cursor')}")
        for unchecked in rep["qc"].get("unchecked_ranges", []):
            add(f"  unchecked range: after {unchecked.get('after') or 'start'} "
                f"through {unchecked.get('through')}")
        for error in rep["qc"].get("coverage_errors", []):
            detail = error.get("detail") or error.get("evaluators") or ""
            add(f"  - {error.get('code')}: {detail}")
        incomplete_evaluators = [
            item for item in rep["qc"].get("evaluators", [])
            if item.get("status") in ("skipped", "disabled", "not_requested")
        ]
        for item in incomplete_evaluators:
            add(f"  evaluator {item.get('id')}: {item.get('status')} "
                f"(eligible {item.get('eligible')})")
        if rep["qc"].get("live_only_unchecked"):
            add("  live-only evaluators not proven complete:")
            for check_id in rep["qc"]["live_only_unchecked"]:
                add(f"    - {check_id}")
        add("")

    historical = rep["qc"].get("historical_findings", [])
    if historical:
        add(f"## Historical findings ({len(historical)})")
        for finding in historical:
            add(f"  {finding['status']} {finding['check']}: {finding['detail']}")
        add("")

    # --- 2. what the seats reported ------------------------------------
    u = rep["uxr"]
    add(f"## From the seats ({u['signals_total']} signal"
        f"{'' if u['signals_total'] == 1 else 's'})")
    if u["patterns"]:
        add(f"  Patterns (>= {u['floor']} — worth acting on):")
        for p in u["patterns"]:
            mark = "+" if p["positive"] else "-"
            add(f"    {mark} {p['signal']} ×{p['count']} — {p['means']}; "
                f"seats: {', '.join(p['seats']) or 'unattributed'}"
                f"; beats {', '.join(str(b) for b in p['beats'])} "
                f"[{'/'.join(p['sources'])}]")
    if u["moments"]:
        add(f"  Individual moments (below {u['floor']} — a moment, not a tendency):")
        for p in u["moments"]:
            mark = "+" if p["positive"] else "-"
            add(f"    {mark} {p['signal']} ×{p['count']} — {p['means']}, "
                f"beat(s) {', '.join(str(b) for b in p['beats'])} "
                f"[{'/'.join(p['sources'])}]")
    if not u["patterns"] and not u["moments"]:
        add("  Nothing was classified tonight. That is an absence of data, "
            "not an absence of friction — a GM who did not notice friction")
        add("  cannot have recorded it. The close-out questions are the check "
            "on exactly that blind spot.")
    else:
        add("  (inferred from ordinary speech; advisory always, never a defect)")
    add("")

    if u["stories"]:
        add("## User stories")
        for s in u["stories"]:
            add(f"  • {s['story']}")
            if s.get("said"):
                add(f"      they said: \"{s['said']}\"")
            if s.get("reacting_to"):
                add(f"      reacting to: \"{s['reacting_to']}\"")
        add("")

    if u["debriefs"]:
        add("## What they said at the close")
        for d in u["debriefs"]:
            add(f"  {d['seat']} — asked: {d['question']}")
            add(f"      \"{d['answer']}\"")
        add("")
    else:
        add("## No close-out questions recorded")
        add("  This is the high-confidence half of the lane, and the only "
            "check on the GM's own blind spots.")
        add("  `tablekit debrief` prints them — ask them in your own words, "
            "as part of the close.")
        add("")

    # --- 3. outcome pairs ----------------------------------------------
    if rep["outcomes"]:
        add("## Did it work (outcome pairs)")
        for kind, v in sorted(rep["outcomes"].items()):
            rate = (f"{v['success_rate']:.0%}" if v.get("success_rate") is not None
                    else f"n={v['closed']} — {v.get('note', 'too few to rate')}")
            add(f"  {kind:<10} {v['good']}/{v['closed']} good  ({rate})"
                + (f"  median {v['median_latency_s']}s" if v.get("median_latency_s") else "")
                + (f"  [{v['still_open']} still open]" if v.get("still_open") else ""))
        add("")

    # --- 4. shape of the evening ---------------------------------------
    b, s = rep["beats"], rep["seats"]
    add("## Shape")
    add(_line("GM beats", b.get("beats")))
    add(_line("GM median words", b.get("median_words")))
    add(_line("GM longest beat", b.get("longest_words"), " words"))
    add(_line("GM share of table words", s.get("gm_share_of_table")))
    add(_line("beats per hour", b.get("beats_per_hour")))
    add("")
    add("  seat                 lines   share  longest silence   cued")
    for sid, v in sorted(s.get("seats", {}).items()):
        share = f"{v['share_of_player_words']:.0%}" if v.get("share_of_player_words") is not None else "  —"
        add(f"  {sid:<20} {v['lines']:>5}   {share:>5}   "
            f"{int(v['longest_silence_s'] // 60):>10} min   {v['cued']:>4}")
    add("  (word shares for seats are estimated from message length)")
    add("")

    # --- 5. did the machinery hold -------------------------------------
    t = rep["transport"]
    add("## Machinery")
    add(_line("posts", t["posts"]))
    add(_line("post failures", t["post_failures"]))
    add(_line("median post latency", t["median_post_latency_ms"], " ms"))
    if t.get("rolls_by_route"):
        routes = ", ".join(f"{k} {v}" for k, v in sorted(t["rolls_by_route"].items()))
        add(_line("how rolls arrived", routes))
    add(_line("listener interruptions", t["listener_interruptions"]))
    add(_line("command failures", f"{t['command_failures']}/{t['commands']}"))
    add(_line("routing quarantines", t.get("routing_quarantines", 0)))
    if t.get("routing_quarantine_events", 0) != t.get("routing_quarantines", 0):
        add(_line("quarantine events (history)",
                  t.get("routing_quarantine_events", 0)))
    if t.get("routing_quarantine_reasons"):
        reasons = ", ".join(
            f"{reason} {count}" for reason, count
            in sorted(t["routing_quarantine_reasons"].items()))
        add(_line("quarantine reasons", reasons))
    add(_line("routing advisories", t.get("routing_advisories", 0)))
    if t["malformed_records"]:
        add(_line("MALFORMED records", t["malformed_records"]))
    add("")

    att = rep["qc"]["attention"]
    if att:
        add(f"## Attention items ({len(att)}) — dosage, not boundaries")
        seen = {}
        for f in att:
            seen[f["check"]] = seen.get(f["check"], 0) + 1
        for k, n in sorted(seen.items(), key=lambda kv: -kv[1]):
            add(f"  {k} ×{n}")
        add("")

    if rep.get("parked"):
        add(f"## Parking lot — below the table ({len(rep['parked'])})")
        add("  Noticed during play, deliberately not chased during play.")
        for d in rep["parked"]:
            who = f" [{d['seat']}]" if d.get("seat") else ""
            add(f"  • {d['topic']}{who}: {d['detail']}")
        add("")

    add("No composite score is produced, by design — see report.py.")
    return "\n".join(L)


def exit_code(rep):
    if not rep["enough_data"] or not rep["qc"].get("coverage_complete"):
        return 2
    if (rep["qc"]["defects"]
            or rep["qc"]["attention"]
            or rep.get("transport", {}).get("routing_quarantines", 0)):
        return 1
    return 0


def to_json(rep):
    return json.dumps(rep, indent=1, ensure_ascii=False)
