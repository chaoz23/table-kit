"""The post-session report — five lanes, no score.

There is no composite number at the bottom of this report and there is not
going to be one. A single "table quality: 78" would be read, believed, and
optimised against, and it would be a fiction assembled from a handful of
self-reported markers, some latencies, and a couple of boundary checks. The
report's job is to put the evening's evidence where a human can look at it,
sorted so the parts that are actually load-bearing come first.

What it *will* say plainly:

  * every categorical defect, because those are pass/fail
  * every marker a player dropped, anchored to the beat it was about
  * the user stories those markers imply, in the seat's own framing
  * the questions worth asking at the break, and only the ones still unanswered
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
    markers = [r for r in rows if r.get("type") == "uxr.marker"]
    findings = [r for r in rows if r.get("type") == "qc.finding"]
    beat_text = {i + 1: (b.get("text") or "")[:160] for i, b in enumerate(beats)}

    by_marker = {}
    for m in markers:
        by_marker.setdefault(m["marker"], []).append(m)

    #: Below the floor, a marker is a moment and not a tendency. The report
    #: says which it is holding, every time, rather than leaving the reader to
    #: guess how much weight the number carries.
    patterns, moments = [], []
    for name, group in sorted(by_marker.items(), key=lambda kv: -len(kv[1])):
        info = uxr_mod.MARKERS[name]
        entry = {"marker": name, "dimension": info["dimension"],
                 "meaning": info["meaning"], "count": len(group),
                 "seats": sorted({g.get("seat") for g in group if g.get("seat")}),
                 "beats": [g.get("beat") for g in group]}
        (patterns if len(group) >= min_pattern else moments).append(entry)

    defects = [f for f in findings if f.get("severity") == "defect"]
    return {
        "table": cfg.name if cfg else None,
        "enough_data": len(beats) >= MIN_BEATS,
        "beats": ux_mod.beat_stats(ledger),
        "seats": ux_mod.seat_stats(ledger, cfg),
        "transport": ux_mod.transport_stats(ledger),
        "qc": {
            "defects": [{"check": f["check"], "detail": f.get("detail"),
                         "seat": f.get("seat"), "evidence": f.get("evidence")}
                        for f in defects],
            "attention": [{"check": f["check"], "detail": f.get("detail")}
                          for f in findings if f.get("severity") != "defect"],
        },
        "uxr": {
            "markers_total": len(markers),
            "patterns": patterns,
            "moments": moments,
            "floor": min_pattern,
            "stories": uxr_mod.stories(markers, beat_text),
            "followups": uxr_mod.followups(markers),
        },
        "outcomes": pairs_mod.summary(ledger),
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
        add(f"  ✗ {f['check']}: {f['detail']}")
        if f.get("evidence"):
            add(f"      evidence: {f['evidence']}")
    add("")

    # --- 2. what the seats reported ------------------------------------
    u = rep["uxr"]
    add(f"## From the seats ({u['markers_total']} marker"
        f"{'' if u['markers_total'] == 1 else 's'})")
    if u["patterns"]:
        add(f"  Patterns (>= {u['floor']} reports — worth acting on):")
        for p in u["patterns"]:
            add(f"    !{p['marker']} ×{p['count']} [{p['dimension']}] "
                f"{p['meaning']} — seats: {', '.join(p['seats']) or 'unattributed'}"
                f"; beats {', '.join(str(b) for b in p['beats'])}")
    if u["moments"]:
        add(f"  Individual moments (below {u['floor']} — a moment, not a tendency):")
        for p in u["moments"]:
            add(f"    !{p['marker']} ×{p['count']} [{p['dimension']}] "
                f"at beat(s) {', '.join(str(b) for b in p['beats'])}")
    if not u["patterns"] and not u["moments"]:
        add("  No markers dropped. That is an absence of data, not an absence "
            "of friction — check that the seats know the markers exist.")
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

    if u["followups"]:
        add("## Ask at the break (cause not stated)")
        for f in u["followups"]:
            add(f"  • {f['seat']} (!{f['marker']}, beat {f['beat']}): {f['question']}")
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
