---
name: tablekit
version: 0.5.1
description: >
  Glue and instrumentation for a live tabletop game where some seats are AI
  agents. Use it when you are the GM (or GM's operator) of a hybrid table:
  starting a session, recording beats and cues, tracking who was told what,
  noticing a quiet seat, logging rolls and whether they landed, and closing
  out with a report. Produces one append-only JSONL per session that is both
  the play ledger (dmcheck-compatible) and the telemetry stream. Players
  never learn commands — the table speaks English; only the GM records.
---

# tablekit — run the hybrid table without teaching anyone syntax

The GM narrates however it likes and *records what it understood*: beats,
cues, rolls, inbound speech, check-ins. The session file is the product —
one JSONL that replays the evening for QA, conduct review, and outcome
tracking.

## Three things to remember

1. **There is no player syntax, on purpose.** People talk the way they were
   always going to talk. Never ask a seat to use markers, verbs, or
   commands; never parse player text for keywords. The GM records; players
   just play.
2. **Open pairs are promises.** A `beat --cue` or `roll` opens a pair that
   `inbound`/`consumed` closes. Unclosed pairs at close-out are the
   evening's dropped balls — check `pairs` between scenes.
3. **No composite score exists, by design.** Lanes (qa/qc/ux/uxr/out) are
   reported separately; never sum them into one number.

## Exit codes

`0` = recorded/clean · `1` = a check failed (e.g. `beat`: the cue would be
dropped in transit; `qc`: findings) · `2` = can't do that (`init`: file
exists; `consumed`: id not open). Read the code; the message says which.

## Invocation

```bash
tablekit init                       # write starter table.json (edit it; agent seats need "mention")
tablekit beat "<narration>" --cue rowan     # record a GM beat; opens a cue pair
tablekit inbound --seat rowan --text "..."  # record what a seat said; closes pairs it resolves
tablekit roll --seat rowan "Perception to place the sound"
tablekit consumed <pair-id> [--outcome ...] # close a pair explicitly
tablekit checkin --seat vesh                # noticed a quiet seat
tablekit turn --seat bram [--wait N]        # a seat got the floor
tablekit qc [--json]                        # run the checks now (exit 1 on findings)
tablekit pairs [--json]                     # what's still open
```

Try it with zero setup: `python3 examples/demo_session.py` runs a synthetic
imperfect session and prints its report.

## Worked example

A beat with a cue, the reply, a roll, and a between-beats check:

```bash
tablekit beat "The causeway is coming up out of the water. Rowan, you are first onto the wet stone." --cue rowan
tablekit inbound --seat rowan --text "I go slow, watching the water line."
tablekit roll --seat rowan "Perception to place the sound"
tablekit consumed roll-1785440000-0000-3f2a
tablekit qc
```

`beat` exits 1 if the cue can't be delivered (e.g. an agent seat whose
`mention` is missing from table.json — the config loader refuses such seats
at load, on purpose). `inbound` records who spoke and how much; prose is
not stored by default and is never parsed for commands.

## MUST / MUST NOT

- MUST give every agent seat the literal platform `mention` in table.json —
  a cue that renders as plain text never wakes the agent.
- MUST record `inbound` for every seat utterance you acted on — the ledger
  is only as honest as what the GM logs.
- MUST run `qc` between beats or scenes, not only at the end.
- MUST close the session conversationally (the uxr lane asks seats how it
  felt in ordinary speech) — no survey forms.
- MUST NOT introduce player-facing syntax, verb lists, or bang markers —
  cut in 0.2.0 and enforced by tests; inferred signals replaced them.
- MUST NOT compute a composite session score, or treat lane counts as one.
- MUST NOT store player prose without need; who-spoke-and-how-much is the
  default record.

## Validation checkpoints (self-audit during play)

every cue pair closed or consciously left open · quiet seats checked in ·
rolls consumed with outcomes · qc run since the last scene · close-out done
in-fiction, not as a form.

## Cross-skill workflows (check family)

- **Session retro:** the session JSONL is dmcheck-compatible — run
  `dmcheck run <session>.jsonl --charter <table charter>` after close-out.
- **Live ruling:** when srdcheck adjudicates mid-turn, record the ruling as
  a beat and its dice as `roll`/`consumed` pairs; the ledger becomes the
  precedent trail.
- **Session start:** charactercheck `seatpack --for-dm` supplies each
  seat's numbers; tablekit tracks the evening those numbers live through.

Family contract: [FAMILY.md](https://github.com/chaoz23/srdcheck/blob/main/FAMILY.md).

## Changelog / stale-knowledge deltas

- **0.2.0:** bang markers CUT — replaced by inferred signals + a
  plain-English close-out. If you remember `!markers`, that's stale and
  tests reject reintroducing them.
- **0.4.x:** five lanes stable (qa/qc/ux/uxr/out); uxr inferred from
  ordinary speech during play, asked about conversationally at close.

The kit instruments; it never referees people. Humans own the table.
