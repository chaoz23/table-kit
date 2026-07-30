#!/usr/bin/env python3
"""LIVE CRAFT METER (epic M1 prototype) — my own rates vs the professional
envelope measured over 134h. Reports; never blocks, never scores."""
import json, re, statistics as st, sys
ENV={"median_words":(14,17),"long40":(.12,.19),"question":(.175,.20),
     "roll":(.05,.09),"npc_voice":(.16,.22)}
def meter(beats):
    w=[len(b.split()) for b in beats]; n=len(beats)
    got={"median_words":st.median(w),
         "long40":sum(1 for x in w if x>40)/n,
         "question":sum(1 for b in beats if "?" in b)/n,
         # BUG FOUND IN USE 2026-07-27: the naive detector counted "no roll"
         # and "plain to see, no roll" as CALLING for a roll. A meter that
         # scores declines as calls pushes an agent toward MORE dice - the
         # exact opposite of the correct direction, and against the worst
         # measured defect. Require an actual request, and exclude negations.
         "roll":sum(1 for b in beats
                    if re.search(r"\b(roll|make a|give me a)\b",b,re.I)
                    and not re.search(r"\b(no|without|don't|do not|need|needed)\s+"
                                      r"(a\s+)?rolls?\b",b,re.I))/n,
         "npc_voice":sum(1 for b in beats if '"' in b)/n}
    print(f"CRAFT METER — {n} beats")
    print(f"{'measure':<20}{'me':>9}{'envelope':>14}{'':>10}")
    for k,(lo,hi) in ENV.items():
        v=got[k]
        f=(lambda x:f"{x:.0f}") if k=="median_words" else (lambda x:f"{x:.0%}")
        out="" if lo<=v<=hi else ("  OVER" if v>hi else "  UNDER")
        print(f"{k:<20}{f(v):>9}{f(lo)+'-'+f(hi):>14}{out:>10}")
    return got


# ---------------------------------------------------------------------------
# ATTENTION ENGINE (2026-07-27). A dashboard is not attention. Surfacing five
# numbers mid-scene cost me the ones I was not looking at — question rate
# collapsed to 6% while I was correcting length. So: surface ONE signal, the
# most out-of-band for THIS scene type, and say nothing about the rest.
#
# Scene-conditioned salience: what matters in combat is not what matters in a
# conversation. Weights come from the measured per-scene envelopes.
SALIENCE = {
    "COMBAT":      {"median_words": 1.3, "roll": 1.2, "question": 0.6,
                    "npc_voice": 0.3, "long40": 1.0},
    "SOCIAL":      {"median_words": 1.0, "roll": 1.3, "question": 1.3,
                    "npc_voice": 1.0, "long40": 1.0},
    "EXPLORATION": {"median_words": 1.0, "roll": 0.9, "question": 1.1,
                    "npc_voice": 0.7, "long40": 1.2},
}


def attention(beats, scene="SOCIAL", window=5, resolved=()):
    """Return the ONE thing worth noticing right now, or None if nothing is.
    `resolved` lets a corrected signal stop competing for attention, so the
    engine moves on instead of nagging."""
    import statistics as _st
    b = beats[-window:] if len(beats) > window else beats
    if len(b) < 3:
        return None
    w = [len(x.split()) for x in b]
    got = {"median_words": _st.median(w),
           "long40": sum(1 for x in w if x > 40) / len(b),
           "question": sum(1 for x in b if "?" in x) / len(b),
           "roll": sum(1 for x in b if re.search(r"\b(roll|make a|give me a)\b", x, re.I)
                       and not re.search(r"\b(no|without|don't|need)\s+(a\s+)?rolls?\b", x, re.I)) / len(b),
           "npc_voice": sum(1 for x in b if '"' in x) / len(b)}
    sal = SALIENCE.get(scene.upper(), SALIENCE["SOCIAL"])
    worst, score = None, 0.0
    for k, (lo, hi) in ENV.items():
        if k in resolved:
            continue
        v = got[k]
        if lo <= v <= hi:
            continue
        span = hi - lo or 1
        dev = (lo - v) / span if v < lo else (v - hi) / span
        s = dev * sal.get(k, 1.0)
        if s > score:
            worst, score = k, s
    if not worst:
        return None
    v, (lo, hi) = got[worst], ENV[worst]
    fmt = (lambda x: f"{x:.0f}") if worst == "median_words" else (lambda x: f"{x:.0%}")
    return {"notice": worst, "you": fmt(v), "target": f"{fmt(lo)}-{fmt(hi)}",
            "direction": "shorter/less" if v > hi else "more",
            "scene": scene.upper(), "urgency": round(score, 2)}


# ---------------------------------------------------------------------------
# DEFECT DETECTORS beyond rates (added 2026-07-27 after a second agent was
# tested with this kit). The rate meter is blind to whole classes of failure:
# it measures how MUCH you speak, never WHOSE mouth you put words in. The
# second DM's defects were nothing like the author's, which is exactly why the
# instrument had to grow.
def hard_defects(beat, pc_names=()):
    """Categorical DM failures a rate meter cannot see. Returns a list."""
    out = []
    quoted = re.findall(r'"([^"]{3,})"', beat)
    for pc in pc_names:
        # PC name within ~80 chars before a quote => the DM likely voiced the PC
        for m in re.finditer(r'"[^"]{3,}"', beat):
            before = beat[max(0, m.start() - 80):m.start()]
            if re.search(r'\b' + re.escape(pc) + r'\b', before, re.I):
                out.append(f"VOICED_THE_PC:{pc}")
                break
    if re.search(r"\bif\b[^.]{0,60}\b(then|,)\b[^.]{0,60}\b(if|otherwise)\b", beat, re.I):
        out.append("EXPOSED_INFERENCE_TREE")
    if re.search(r"\b(\w+)\s+or\s+(\w+),?\s*(whichever|either)\b", beat, re.I):
        out.append("DEFERRED_THE_ADJUDICATION")
    if re.search(r"\bi'?ll roll\b", beat, re.I) and pc_names:
        out.append("ROLLED_FOR_THE_PLAYER")
    return sorted(set(out))

USAGE = """craftmeter — the rolling craft numbers, read between beats.

  craftmeter.py <beats.json> [window] [SOCIAL|COMBAT|EXPLORATION] [pc,names]

<beats.json> is a JSON array of the GM lines you have said this session, in
order. The rolling window is the steerable number; the session total is not.
"""

if __name__=="__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0 if len(sys.argv) > 1 else 2)
    beats=json.load(open(sys.argv[1]))
    meter(beats)
    scene = sys.argv[3] if len(sys.argv)>3 else "SOCIAL"
    a = attention(beats, scene)
    print("\n--- ATTENTION (one signal, scene-conditioned) ---")
    print(json.dumps(a, indent=1) if a else "  nothing out of band — carry on")
    pcs = sys.argv[4].split(",") if len(sys.argv)>4 else []
    hd = [(i, hard_defects(b, pcs)) for i,b in enumerate(beats)]
    hd = [(i,d) for i,d in hd if d]
    if hd:
        print("\n--- HARD DEFECTS (categorical; a rate meter cannot see these) ---")
        for i,d in hd: print(f"  beat {i}: {', '.join(d)}")
    # SECOND REQUIRED FIX (found in use, 2026-07-27): a cumulative session rate
    # cannot steer. It includes the correctly-long opening and averages away my
    # current trajectory. What a DM needs mid-session is a ROLLING WINDOW.
    k=int(sys.argv[2]) if len(sys.argv)>2 else 3
    if len(beats)>k:
        print(f"\n--- rolling window: last {k} beats (this is the steerable number) ---")
        meter(beats[-k:])
