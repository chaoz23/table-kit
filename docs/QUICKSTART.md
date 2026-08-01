# Quickstart

A running instrumented table in about ten minutes. The first two steps need no
chat platform at all.

## 1. See what it produces

```bash
pip install tablekit
python3 examples/demo_session.py
```

That runs a synthetic seventy-minute session — deliberately an imperfect one —
and prints its report. Nothing to configure, no network.

## 2. Make a config

```bash
tablekit init          # writes table.json
```

Edit it. The loader rejects unknown fields and bad types, requires every
identity key to resolve to one seat, and requires **every agent seat to carry
the literal mention its chat platform needs**:

```json
{"id": "vesh", "display": "Vesh", "kind": "agent", "mention": "<@1234567890>"}
```

Leave it out and the config is refused at load, on purpose — see
[TRANSPORT.md](TRANSPORT.md) for the evening that rule cost.

Relative `data_dir` values are anchored to the directory containing
`table.json`, in both the Python tools and Discord listener. Session names are
opaque IDs such as `session-1`, not paths. Comment-only JSON keys may start
with `_`, as they do in the generated example.

## 3. Nothing to teach the table

There is no marker card to pin, no syntax to explain, no commands for anyone to
learn. That is the design, not an omission — see the README. People talk the
way they were always going to talk, and the GM records what it understood.

## 4. Run a session

Without a chat platform, drive it by hand:

```bash
tablekit beat "The causeway is coming up out of the water. Rowan, you are first onto the wet stone." --cue rowan
# Copy the cue pair ID printed by beat:
tablekit inbound --seat rowan --pair cue-1785440000000-7a04fb81b49c49e68b72f8d21ba117da --text "I go slow, watching the water line."
tablekit roll --seat rowan "Perception to place the sound"
tablekit consumed roll-1785440000000-4f3c2a107af149cc91f62331dbf67adc
tablekit qc                 # between beats — one line, or a defect
```

`inbound` closes at most one cue/check-in obligation and requires `--pair ID`
to correlate it, even when that is the seat's only open obligation. Without a
pair ID, the inbound is recorded and the unresolved match remains visible as
`missing_correlation`. Blank text acknowledges nothing. A typed roll result
remains advisory—use the returned roll ID with `consumed` after correlating and
confirming it. A verified structured roll relay can resolve it only when the
host supplies that explicit pair correlation.

When someone says something that tells you how the evening is landing, record
it with their own words attached:

```bash
tablekit signal --seat rowan --kind pacing --quote "can we get to the fight"
tablekit signal --seat vesh  --kind spotlight --quote "finally, this is what she does"
```

Buckets: `pacing`, `floor`, `comprehension`, `resonance`, `spotlight`,
`continuity`. The quote is mandatory — an inference nobody can check against
what was actually said is indistinguishable from one the machine invented.

With Discord, let the listener handle the inbound half:

```bash
export TABLE_BOT_TOKEN=...
node transport/discord/listen.mjs table.json | python3 -m tablekit.ingest &
```

Enable the bot's privileged `MESSAGE_CONTENT` intent in Discord's Developer
Portal first. The listener requests that capability and exits 2 with a typed
stderr diagnosis if Discord rejects it; REST cannot bypass the same content
restriction. The bot user ID comes from Discord's READY event, not config.

The current listener can Resume recoverable disconnects during one process,
but it has no downstream commit acknowledgment, durable checkpoint, or
cold-start backfill yet. Treat it as live streaming, not proof of complete
session capture; see [TRANSPORT.md](TRANSPORT.md#3-resume-is-not-a-durable-checkpoint).

The listener must supply stable message IDs. Every ID is durably receipted
before routing, including quarantined messages; missing IDs cannot resolve
state. Unknown identities and missing or invalid pair correlations stay visible
in `qa.route` instead of being guessed.

Then post through the helper so mentions are guaranteed and beats are recorded:

```python
from tablekit import load, post
from tablekit.events import Ledger

cfg = load()
led = Ledger(cfg.ledger_path())
post.post(cfg, led, "Vesh, the water has gone quiet around your ankles.", cue="vesh")
```

## 5. Close the evening

Ask the close-out questions — in your own words, as part of the ending, not as
a form:

```bash
tablekit debrief            # prints what is worth asking
tablekit debrief --seat rowan --q "anything drag tonight?" --a "the chapel bit went long"
```

This half matters more than the inference does. A GM that did not notice
friction cannot have recorded any, so asking is the only check on its own blind
spots.

```bash
tablekit sweep      # name the outcomes of anything still hanging
tablekit report     # exit 0 clean, 1 findings, 2 not enough happened
tablekit report --json --out reports/session-1.json
```

## Between sessions

After three or four sessions you will have enough closed outcome pairs to
retune the thresholds in `table.json` against your own table instead of the
shipped defaults. That is the intended path; the defaults are a starting
position, not a standard.
