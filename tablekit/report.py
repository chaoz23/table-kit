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

from . import pairs as pairs_mod
from . import ux as ux_mod
from . import uxr as uxr_mod

#: A session shorter than this has nothing to report on, and saying "no
#: defects found" about it would be a lie of omission.
MIN_BEATS = 3


def build(ledger, cfg=None, min_pattern=uxr_mod.MIN_PATTERN):
    rows = ledger.read()
    beats = ledger.beats()
    signals = [r for r in rows if r.get("type") == "uxr.signal"]
    debriefs = [r for r in rows if r.get("type") == "uxr.debrief"]
    findings = [r for r in rows if r.get("type") == "qc.finding"]
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

    # Deduplicate. QC runs between beats, so a standing defect is recorded
    # once per run — by the end of a session that is dozens of copies of one
    # problem. It is one defect that persisted, and saying so (with how many
    # checks it survived) is more useful than listing it thirty times.
    seen, defects = {}, []
    for f in findings:
        if f.get("severity") != "defect":
            continue
        key = (f.get("check"), f.get("seat"), f.get("detail"))
        if key in seen:
            seen[key]["seen"] += 1
            continue
        entry = {"check": f["check"], "detail": f.get("detail"),
                 "seat": f.get("seat"), "evidence": f.get("evidence"),
                 "seen": 1}
        seen[key] = entry
        defects.append(entry)
    return {
        "table": cfg.name if cfg else None,
        "enough_data": len(beats) >= MIN_BEATS,
        "beats": ux_mod.beat_stats(ledger),
        "seats": ux_mod.seat_stats(ledger, cfg),
        "transport": ux_mod.transport_stats(ledger),
        "qc": {
            "defects": defects,
            "attention": [{"check": f["check"], "detail": f.get("detail")}
                          for f in findings if f.get("severity") != "defect"],
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
        add("This is a refusal, not a clean bill of health.")
        return "\n".join(L)

    # --- 1. defects first: the only pass/fail content in the report -----
    d = rep["qc"]["defects"]
    add("## Defects" if d else "## Defects — none")
    for f in d:
        persisted = (f" (still open across {f['seen']} checks)"
                     if f.get("seen", 1) > 1 else "")
        add(f"  ✗ {f['check']}: {f['detail']}{persisted}")
        if f.get("evidence"):
            add(f"      evidence: {f['evidence']}")
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
    if not rep["enough_data"]:
        return 2
    if rep["qc"]["defects"]:
        return 1
    return 0


def to_json(rep):
    return json.dumps(rep, indent=1, ensure_ascii=False)
