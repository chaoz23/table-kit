# table-kit

**Run a tabletop game where some of the seats are AI agents — and find out
afterwards how it actually went.**

Three deterministic referees already exist for the *rules* of such a table:
[srdcheck](https://github.com/chaoz23/srdcheck) for rules verdicts,
[charactercheck](https://github.com/chaoz23/charactercheck) for character
sheets, [dmcheck](https://github.com/chaoz23/dmcheck) for table conduct. They
are libraries. The thing that makes them a *table* — the chat transport, the
session file, the game state, and the instrumentation that turns an evening
into something you can learn from — is what this repo is.

It is an **example, not a framework**. Small enough to read in one sitting, and
it expects to be edited.

```bash
pip install tablekit
tablekit init                      # writes table.json
python3 examples/demo_session.py   # a whole synthetic session + its report
```

## The rule that governs the whole design

**The table speaks English. Nobody at it learns a syntax.**

Text RPGs solved this badly for forty years. Zork and *Moria* and everything
after them handed you a verb list, because a parser in 1980 could not
understand a sentence. That vocabulary was never a design choice — it was a
workaround for a missing capability, and it is exactly what makes those games
feel like operating a computer instead of sitting with someone who is running a
world for you.

The capability is no longer missing. So an agent GM that asks people to learn
commands has voluntarily reproduced the limitation and thrown away the only
thing it has over a parser game.

Concretely, in this repo: there are no chat commands, no `!tokens`, no
keywords, no "advanced mode". Whatever someone writes in the channel is
dialogue. `tests/test_no_player_commands.py` enforces that, because a stated
principle drifts and a test does not.

An earlier version of this kit got this wrong — it shipped a six-token feedback
vocabulary. It was cut in 0.2.0 and the reasoning is preserved in
`tablekit/uxr.py`.

## What it does

Everything a session produces goes into **one append-only JSONL file**, which
is simultaneously the play ledger and the telemetry stream. `dmcheck` can be
pointed straight at it with no export step.

| lane  | answers                                | how |
|-------|----------------------------------------|-----|
| `qa`  | did the machinery work?                | automatic |
| `qc`  | was the refereeing correct?            | automatic |
| `ux`  | what did the seat's evening look like? | automatic |
| `uxr` | how did it *feel* from a seat?         | inferred from ordinary speech, then asked about at the close |
| `out` | did any of it actually work?           | intent → payoff pairs |

Inbound handling is deliberately fail-closed. A source-native message ID is
committed before routing and deduplicates replays, including messages that are
rejected or quarantined. An inbound can resolve **at most one** typed obligation:
an explicit `pair_id` wins, otherwise exactly one compatible open pair must
exist for that seat. Blank messages acknowledge nothing; ambiguous, unknown,
cross-seat, and cross-kind correlations remain open and are surfaced in
`qa.route`.

Identity fallback is exact after Unicode/case/whitespace normalization, never a
substring guess (`Will` does not capture `William`). Unknown speakers remain
`unknown`. Relayed rolls additionally require a configured relay name, an
actual bot flag, exact sheet ID or exact configured alias, a numeric result,
and a plausible declared natural-die range. Ordinary prose such as `14` or
`nat 20` is **advisory only**; damage, healing, hit points, movement, and other
non-roll quantities do not auto-resolve a roll.

### The `uxr` lane

This is the one that does not exist elsewhere, and the one the no-syntax rule
shapes most. A transcript of a great session and a transcript of a session
everyone endured look about the same — whether a description landed, whether
someone was quietly lost, whether a player got to do the thing their character
exists for, none of it leaves a trace.

So it is captured two ways, both of them in plain English:

**Inferred during play.** "Can we get moving" *is* the signal. The agent GM is
already reading every line; classification does not need a new interface, it
needs the GM to record what it already understood. Six internal buckets —
pacing, floor, comprehension, resonance, spotlight, continuity — never surfaced
to anyone at the table. Every inferred signal stores the player's own words, so
the inference is auditable by whoever was in the room.

**Asked at the close.** Two or three questions in your own words, the way any
decent GM already ends a night. `tablekit debrief` prints them.

The second half is not optional politeness — it is the structural check on the
first. A GM that does not notice friction cannot record friction, so inference
alone inherits exactly the blind spots the lane was built to route around. An
independent model pass over the same transcript (`--source local`) is the other
check; where it and the GM disagree is itself the interesting part.

## The report

```bash
tablekit report
```

Defects first, then what the seats said, then whether the craft moves worked,
then the shape of the evening, then whether the plumbing held.

```
## Defects
  ✗ undeliverable_cue: beat 4: cue addresses agent seat 'Vesh' but does not
    contain its literal mention — likely to be dropped in transit
  ✗ seat_quiet: Vesh has not said anything for 49 minutes and has not been
    checked on

## From the seats (5 signals)
  Patterns (>= 3 — worth acting on):
    - pacing ×3 — this stretch felt slow from that seat; seats: brae, rowan;
      beats 5, 6, 6 [dm/local]

## Did it work (outcome pairs)
  cue        3/4 good  (75%)  median 60.0s
  roll       0/1 good  (n=1 — too few to state a rate; counts shown instead)
```

Note the ordering: the undeliverable cue is reported *before* the silence it
caused. And note what is absent — there is no score at the bottom, and there
is not going to be one.

## Three things this kit will not do

**1. Give players a command syntax.** Above. It is a product non-goal, not a
backlog item.

**2. Let an inference accuse anyone.** Signals classified from speech are
advisory in every path and can never become a defect. Something a model
*thought* someone meant does not get to be a finding.

**3. State a rate from a handful of observations.** Below three occurrences,
the report lists individual moments with their beat numbers and refuses to
describe a tendency. This is a correction of a real error: one player asking
what a word meant was once generalised into "unfamiliar diction fails at this
table", and their actual position was the opposite.

Findings come in two severities and the difference is load-bearing: **defects**
are boundary crossings (pass/fail, worth interrupting for); **attention** items
are dosage readings (real signal, no boundary crossed).

## Layout

```
tablekit/        the Python package — events, config, lanes, report, CLI
transport/       Discord gateway listener (JSON out; knows nothing of the schema)
engine/          optional boardgame.io combat state: initiative, HP, seeded dice
bootstrap/       a first-session kit for an agent about to GM for the first time
examples/        a full synthetic session you can run right now
docs/            INSTRUMENTATION.md is the one to read
```

## Exit codes

Same convention as the sibling projects: **0** clean, **1** findings worth
looking at, **2** refused. Exit 2 is an honest refusal — bad input, or not
enough happened to say anything — and it is deliberately distinct from "the
session was fine." An unresolved routing quarantine is exit 1 even when it
safely prevented a bad state transition; operator work still remains.

## Docs

- [docs/QUICKSTART.md](docs/QUICKSTART.md) — a table running in about ten minutes
- [docs/INSTRUMENTATION.md](docs/INSTRUMENTATION.md) — the lanes, the event
  schema, and the provenance of every default
- [docs/TRANSPORT.md](docs/TRANSPORT.md) — mandatory mentions, Discord
  capability and recovery boundaries, the relay tax, and the other things that
  cost an evening to learn
- [docs/DATA_SAFETY.md](docs/DATA_SAFETY.md) — paths, permissions, durability,
  plaintext storage, and the current integrity boundary
- [bootstrap/CORE.md](bootstrap/CORE.md) — if an agent is about to GM for the
  first time, this is the page to hand it

## For agents

`tool.json` describes the command surface — all of it operator-side, none of it
touched by anyone at the table. `tablekit schema` prints the event schema,
typed pair outcomes, signal buckets and exit codes as JSON. The session file is
JSONL with one object per line and a documented type registry; reading it needs
no library.
Treat it as table-private data and as evidence, not a cryptographically trusted
audit log; the exact boundary is documented in
[DATA_SAFETY.md](docs/DATA_SAFETY.md).

MIT.
