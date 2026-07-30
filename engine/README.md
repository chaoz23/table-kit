# engine — optional combat state

A small [boardgame.io](https://boardgame.io) server that owns the parts of a
fight nobody should be tracking in their head: initiative order, hit points,
conditions, and **seeded, replayable dice**. It is optional — the rest of the
kit works fine at a table that resolves combat by hand.

The topology is deliberate: **one engine player, the GM.** Players express
intent in chat and the GM submits moves. That is the single-writer ledger
model, and it exists because a shared-writer setup makes "who changed this
number" unanswerable exactly when it matters.

```bash
npm install boardgame.io
node server.cjs &                      # loopback only, state survives restarts
node move.mjs state
node move.mjs act hero attack goblin
node story.mjs init ../examples/example-module
node story.mjs set door_open true
```

`story.mjs` is the other half: the adventure's flags, validated against the
module's spine on every change. Gates constrain *what may become true* and
endings constrain *how the story may resolve* — neither constrains which path
the table takes to get there. Freeform play, guarded outcomes.

Mirror the engine log into the session file with the ledger tap:

```python
from tablekit import engine
from tablekit.events import Ledger
engine.tap(Ledger("table-data/session.jsonl"), engine.state_from("node engine/move.mjs state"))
```

The tap is idempotent and keeps its position **inside the session file**, not
in a sidecar — a tap that stores its position separately loses its place the
first time a session resumes from a different directory, and then silently
re-narrates twenty minutes of combat.
