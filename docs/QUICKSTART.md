# Quickstart

A running instrumented table in about ten minutes. You can do the first two
steps with no chat platform at all.

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

Edit it. The only rule the loader enforces is that **every agent seat carries
the literal mention its chat platform needs**:

```json
{"id": "vesh", "display": "Vesh", "kind": "agent", "mention": "<@1234567890>"}
```

Leave it out and the config is refused at load, on purpose — see
[TRANSPORT.md](TRANSPORT.md) for the evening that rule cost.

## 3. Give the table the marker card

```bash
tablekit markers
```

Paste that into the channel and pin it. Players type these inline, mid-scene,
without stopping play. If nobody uses them the report will say so — "no markers
dropped" is reported as an absence of data, not an absence of friction.

## 4. Run a session

Without a chat platform, drive it by hand:

```bash
tablekit beat "The causeway is coming up out of the water. Rowan, you are first onto the wet stone." --cue rowan
tablekit inbound --seat rowan --text "I go slow, watching the water line. !yes"
tablekit roll --seat rowan "Perception to place the sound"
tablekit consumed roll-1785440000
tablekit qc                 # between beats — one line, or a defect
```

With Discord, let the listener do the inbound half:

```bash
export TABLE_BOT_TOKEN=...
node transport/discord/listen.mjs table.json | python3 -m tablekit.ingest &
```

and post through the helper so mentions are guaranteed and beats are recorded:

```python
from tablekit import load, post
from tablekit.events import Ledger

cfg = load()
led = Ledger(cfg.ledger_path())
post.post(cfg, led, "Vesh, the water has gone quiet around your ankles.", cue="vesh")
```

## 5. Close the evening

```bash
tablekit sweep      # name the outcomes of anything still hanging
tablekit report     # exit 0 clean, 1 findings, 2 not enough happened
tablekit report --json --out reports/session-1.json
```

## Between sessions

The report's **ask-at-the-break** list is the highest-value part and it decays
fast — ask those questions while people still remember the moment.

After three or four sessions you will have enough closed outcome pairs to
retune the thresholds in `table.json` against your own table instead of the
shipped defaults. That is the intended path; the defaults are a starting
position, not a standard.
