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

It is an **example, not a framework**. It is small enough to read in one
sitting and it expects to be edited.

```bash
pip install tablekit
tablekit init                 # writes table.json
tablekit markers              # the card you paste at the table
python3 examples/demo_session.py   # a whole synthetic session + its report
```

## The problem it solves

Running a hybrid table works fine for about twenty minutes. Then the specific
failures start, and every one of them is invisible while it is happening:

- **A cue that is never delivered.** Agent chat bridges commonly drop
  bot-to-bot messages that lack a literal mention. You address an agent seat by
  name, the message posts, the API returns 200, and the seat never receives it.
  Nothing errors. You conclude the agent is thinking.
- **A seat nobody addressed for forty minutes.** Agents will happily carry a
  scene to its climax while a human player waits for an opening that never
  comes. Read the transcript afterwards and it looks like a good scene.
- **A roll called and never consumed.** Someone rolls, the number lands in
  chat, the moment moves on, and the outcome is never narrated.
- **A ledger only the GM is checking.** Every player who benefits from an error
  has an incentive to say nothing about it.

And underneath all of those, the real one: **a transcript of a great session
and a transcript of a session everyone endured look about the same.** Whether
a description landed, whether someone was bored, whether a player got to do
the thing their character is *for* — none of it leaves a trace.

## What it does

Everything a session produces goes into **one append-only JSONL file**, which
is simultaneously the play ledger and the telemetry stream. `dmcheck` can be
pointed straight at it, with no export step.

Four lanes of instrumentation, plus outcome pairs:

| lane  | answers                          | how          |
|-------|----------------------------------|--------------|
| `qa`  | did the machinery work?          | automatic    |
| `qc`  | was the refereeing correct?      | automatic    |
| `ux`  | what did the seat's evening look like? | automatic |
| `uxr` | how did it *feel* from a seat?   | **players type one token** |
| `out` | did any of it work?              | intent → payoff pairs |

The `uxr` lane is the one that does not exist anywhere else. Six markers, typed
inline in chat, no interruption to play:

```
!huh    I did not follow that            !drag   this is slow for me
!wait   I wanted in, the moment passed    !mine   I got to do my thing
!yes    that landed                       !off    that contradicts something
```

They cost nothing to drop mid-scene, they anchor to the beat that caused them,
and they turn into user stories in the report:

```
• As brae, I wanted to follow the scene, but something in it did not parse (beat 6).
    they said: "what's a gorse"
    reacting to: "Inside, thirty-one cuts in the doorframe, and the rope is still..."
```

## The report

```bash
tablekit report
```

Defects first, then what the seats said, then whether the craft moves worked,
then the shape of the evening, then whether the plumbing held.

```
## Defects
  ✗ undeliverable_cue: beat 4: cue addresses agent seat 'Vesh' but does not
    contain its literal mention <@999...> — likely to be dropped in transit
  ✗ seat_quiet: Vesh has not said anything for 49 minutes and has not been
    checked on

## From the seats (8 markers)
  Patterns (>= 3 reports — worth acting on):
    !drag ×3 [pacing] slow for me right now — seats: brae, rowan; beats 6, 7, 7
  Individual moments (below 3 — a moment, not a tendency):
    !yes ×2   !mine ×1   !huh ×1   !off ×1

## Did it work (outcome pairs)
  cue        3/4 good  (75%)  median 60.0s
  roll       0/1 good  (n=1 — too few to state a rate; counts shown instead)
```

Note the order: the undeliverable cue is reported *before* the silence it
caused. And note what is missing — there is no score at the bottom, and there
is not going to be one.

## Two rules this kit will not bend

**1. A marker is evidence for a pattern, never a verdict on a beat.**

This is a correction of a real mistake. One player asking "what's a gorse" was
once read as "unfamiliar words fail at this table," and the rule written from
it was wrong — their actual position was *"I like the new words, sometimes I
will have to ask."* One report of friction tells you a moment had friction. It
does not tell you the cause and it certainly does not generalise to a
preference. So anything below three occurrences is reported as individual
moments with their beat numbers, never as a rate or a tendency.

**2. Ambiguity produces silence, not an accusation.**

Every check requires positive evidence. Silence from the GM is not a finding,
because a GM deliberately yielding the floor and a GM who has wandered off look
identical in a log — and at professional tables, the yielded floor is the
common case by a wide margin. A checker that cries wolf during play gets
ignored inside twenty minutes, and then it is just noise with a maintenance
cost.

Findings come in two severities and the difference is load-bearing:
**defects** are boundary crossings (pass/fail, worth interrupting for);
**attention** items are dosage readings (real signal, no boundary crossed).

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
session was fine."

## Docs

- [docs/QUICKSTART.md](docs/QUICKSTART.md) — a table running in about ten minutes
- [docs/INSTRUMENTATION.md](docs/INSTRUMENTATION.md) — the four-lane contract,
  the event schema, and what every default was derived from
- [docs/TRANSPORT.md](docs/TRANSPORT.md) — mandatory mentions, the relay tax,
  cursor ownership, and the other things that cost an evening to learn
- [bootstrap/CORE.md](bootstrap/CORE.md) — if an agent is about to GM for the
  first time, this is the page to hand it

## For agents

`tool.json` at the repo root describes the command surface. `tablekit schema`
prints the event schema, pair kinds, marker vocabulary and exit codes as JSON.
The session file is JSONL with one object per line and a documented type
registry; reading it needs no library.

MIT.
