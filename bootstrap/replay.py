#!/usr/bin/env python3
"""Blind DM replay harness — self-evaluation against a professional DM.

Protocol (forced-decoding, re-anchored):
  For each GM decision point i, emit the REAL transcript context up to i
  (which includes the real GM's earlier lines, so the timeline never drifts)
  plus the player turns awaiting a response. The GM's actual line at i is
  written to a SEPARATE answers file that the agent must not open until its
  own response is committed to disk.

  Because block N+1's context contains the real GM lines for block N's
  decision points, reading forward is itself the reveal — the agent is blind
  exactly once per decision, which is the property we want.

Bring your own transcript: JSONL of {ts, author, content}. Any recorded
session works — one of your own, or a published actual-play you have labelled.
The harness only needs to know which speaker is the GM.

Usage:
  replay.py build <transcript.jsonl> <start_sec> <dur_sec> <outdir> --gm NAME
                                                              [--per-block N]
  replay.py compare <outdir>          # after my_NN.json files exist
"""
import glob
import json
import os
import re
import statistics as st
import sys

CTX = 14          # messages of context shown before each decision point
MINWORDS = 8      # only score substantive GM turns, not one-word acks


def load(path):
    """A speaker-labelled transcript: JSONL of {ts, author, content}.

    Bring your own. Any recorded session works — a podcast transcript you have
    labelled, a previous session of your own, a published actual-play. The
    harness needs only to know which lines are the GM's.
    """
    M = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = json.loads(line)
            M.append({"ts": m.get("ts"),
                      "author": m.get("author") or m.get("speaker") or "",
                      "content": m.get("content") or m.get("text") or ""})
    if not M:
        raise SystemExit(f"{path}: no messages found")
    if M[0]["ts"] is None:
        # No timestamps: fall back to message index as a pseudo-clock so the
        # start/duration window still selects a contiguous stretch.
        for i, m in enumerate(M):
            m["ts"] = float(i)
    return M


def build(path, start, dur, outdir, per_block=12, gm=None, seats=None):
    M = load(path)
    if not gm:
        raise SystemExit("build: --gm NAME is required (which speaker is the GM?)")
    if gm not in {m["author"] for m in M}:
        raise SystemExit(
            f"build: no speaker named {gm!r} in {path}. Speakers present: "
            + ", ".join(sorted({m['author'] for m in M})[:12]))
    seats = set(seats) if seats else {m["author"] for m in M if m["author"] != gm}
    t0 = M[0]["ts"]
    win = [i for i, m in enumerate(M)
           if m["ts"] and start <= (m["ts"] - t0) < start + dur]
    lo, hi = win[0], win[-1]
    cand = [i for i in range(lo, hi)
            if M[i]["author"] == gm
            and len(M[i]["content"].split()) >= MINWORDS
            and any(M[j]["author"] in seats for j in range(max(lo, i - 3), i))]
    # BLINDNESS CONSTRAINT (fix, 2026-07-26): decision points must be farther
    # apart than the context window, or decision N's real answer appears
    # inside decision N+1's context and the replay stops being blind.
    dps, last = [], -10**9
    for i in cand:
        if i - last > CTX:
            dps.append(i)
            last = i
    os.makedirs(outdir, exist_ok=True)
    meta = {"episode": path, "gm": gm, "decision_points": len(dps),
            "window_min": round(dur / 60, 1), "blocks": 0}
    for b, s in enumerate(range(0, len(dps), per_block)):
        chunk = dps[s:s + per_block]
        lines = [f"# BLOCK {b:02d} — you are the DM. Respond to each decision point.",
                 f"# Context is the real transcript. Write your line BEFORE reading ahead.",
                 ""]
        ans = {}
        for dp in chunk:
            lines.append(f"===== DECISION {dp} =====")
            lines.append("--- context ---")
            for j in range(max(0, dp - CTX), dp):
                who = M[j]["author"]
                tag = "GM" if who == gm else who
                lines.append(f"[{tag}] {M[j]['content']}")
            lines.append("--- YOUR LINE AS DM (respond to the above) ---")
            lines.append("")
            ans[str(dp)] = M[dp]["content"]
        open(f"{outdir}/prompts_{b:02d}.txt", "w").write("\n".join(lines))
        json.dump(ans, open(f"{outdir}/answers_{b:02d}.json", "w"), indent=1)
        meta["blocks"] = b + 1
    json.dump(meta, open(f"{outdir}/meta.json", "w"), indent=1)
    print(json.dumps(meta, indent=1))


ROLL = re.compile(r"\b(roll|make a|give me a|check|save|d20)\b", re.I)
NPCQ = re.compile(r'"')
QUES = re.compile(r"\?")


def feat(text):
    return {"words": len(text.split()),
            "asks_roll": bool(ROLL.search(text)),
            "npc_voice": bool(NPCQ.search(text)),
            "question": bool(QUES.search(text))}


def compare(outdir):
    meta = json.load(open(f"{outdir}/meta.json"))
    mine, real = {}, {}
    for f in sorted(glob.glob(f"{outdir}/my_*.json")):
        mine.update(json.load(open(f)))
    for f in sorted(glob.glob(f"{outdir}/answers_*.json")):
        real.update(json.load(open(f)))
    keys = [k for k in mine if k in real]
    if not keys:
        print("no responses yet")
        return
    mf = [feat(mine[k]) for k in keys]
    rf = [feat(real[k]) for k in keys]
    print(f"BLIND DM REPLAY — {len(keys)} decision points, {meta['window_min']}min window")
    print(f"episode: {meta['episode']}\n")
    print(f"{'measure':<26}{'ME':>10}{'REAL GM':>10}{'delta':>10}")
    print("-" * 56)
    for name, fn in [("median words", lambda F: st.median([x["words"] for x in F])),
                     ("mean words", lambda F: st.mean([x["words"] for x in F])),
                     ("calls for a roll %", lambda F: 100 * sum(x["asks_roll"] for x in F) / len(F)),
                     ("uses NPC voice %", lambda F: 100 * sum(x["npc_voice"] for x in F) / len(F)),
                     ("asks a question %", lambda F: 100 * sum(x["question"] for x in F) / len(F))]:
        a, b = fn(mf), fn(rf)
        print(f"{name:<26}{a:>10.1f}{b:>10.1f}{a - b:>+10.1f}")
    # per-decision divergence flags for qualitative review
    div = []
    for k, a, b in zip(keys, mf, rf):
        flags = []
        if a["asks_roll"] and not b["asks_roll"]:
            flags.append("I_rolled_they_didnt")
        if b["asks_roll"] and not a["asks_roll"]:
            flags.append("they_rolled_I_didnt")
        if a["words"] > b["words"] * 2 + 5:
            flags.append("I_was_much_longer")
        if b["words"] > a["words"] * 2 + 5:
            flags.append("they_were_much_longer")
        if a["npc_voice"] != b["npc_voice"]:
            flags.append("npc_voice_differs")
        if flags:
            div.append((k, flags))
    print(f"\ndivergences on {len(div)}/{len(keys)} decisions:")
    from collections import Counter
    c = Counter(f for _, fl in div for f in fl)
    for k, v in c.most_common():
        print(f"   {k:<22}{v:>4}  ({v/len(keys):.0%} of decisions)")
    json.dump([{"id": k, "flags": f} for k, f in div],
              open(f"{outdir}/divergences.json", "w"), indent=1)


def _opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        args.pop(i)
        return args.pop(i) if i < len(args) else default
    return default


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    cmd = argv.pop(0)
    if cmd == "build":
        gm = _opt(argv, "--gm")
        per_block = int(_opt(argv, "--per-block", 12))
        seats = _opt(argv, "--seats")
        if len(argv) < 4:
            print(__doc__)
            sys.exit(2)
        build(argv[0], float(argv[1]), float(argv[2]), argv[3],
              per_block=per_block, gm=gm,
              seats=seats.split(",") if seats else None)
    elif cmd == "compare":
        if not argv:
            print(__doc__)
            sys.exit(2)
        compare(argv[0])
    else:
        print(__doc__)
        sys.exit(2)
