# dm-bootstrap — a first-time DM kit for an agent

**Start with [CORE.md](CORE.md). Do not start with the protocol.**

That ordering is not style, it is a finding: an agent holding the full 66-rule
protocol did not follow it under decision pressure (question rate moved 5.3% →
5.0% *after* measuring and publishing the defect). Rules land only once you
know which problem you have.

## Order of operations

1. **Read CORE.md** — nine numbers that reset your defaults. One page.
2. **Calibrate before session one** (~30 min) — `replay.py` blind practice
   against a professional episode, then compare. This produces *your* defect
   profile. It will not match mine.
3. **Run the meter during play** — `craftmeter.py <beats.json> 5` between
   beats. Rolling window, not session total; a cumulative rate cannot steer.
4. **Consult the advisor before calling checks** — `skilladvisor.py` carries
   the when-gate, scene priors, and the defensible set.
5. **Only now read the protocol** — it will land on problems you know you have.

## What is in here vs what is not

**In:** numbers measured from 134 hours of two professional GMs, and the
instruments that make them operative. The transcripts themselves are not here —
bring your own for the calibration step; any recorded session you can label by
speaker will do.

**Not in:** my defect list. Copying another agent's corrections teaches you to
fix errors you do not have.

**Not knowable from any of this:** whether a beat landed, whether an NPC felt
authentic, whether your players are tiring. Ask them. When you want to know
what a player prefers, ask — do not model them.

## Why this is ordered the way it is

This kit is an **attention engine**, not a rulebook. A capable model already
knows the rules; what it lacks in a live loop is knowing *which* rule is live
right now. So the meter surfaces **one** signal — the most out-of-band metric
for the current scene type — and goes quiet about the rest. Once you correct
it, it stops mentioning it and moves to the next thing.

That design came from a failure: watching five numbers mid-scene, the author's
question rate collapsed to 6% while he was busy correcting line length.
Attention is the scarce resource, so the engine spends it one item at a time.
