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
ledger = Ledger("table-data/session.jsonl")
written = engine.tap(ledger, engine.state_from("node engine/move.mjs state"))

# After the table was actually told about one exact returned engine event:
event = next(row for row in written if "engine_log_index" in row)
engine.acknowledge_narration(ledger, event, evidence="discord-message-id")
```

The ingestion and narration cursors are separate. The tap is idempotent and
derives its ingestion position from contiguous, fingerprinted engine events
that were durably appended **inside the session file**. It never advances from
`log_len` alone. A `log_tail` must contain the next expected index; if history
was compacted, reset, forked, reordered, or is behind the local cursor, `tap`
raises a typed `EngineSyncError` and leaves the cursor where it was.

`acknowledge_narration` accepts an exact event returned by `tap`, verifies its
index and content fingerprint against the ledger, and only then advances
`narrated_through`. Supplying a scalar `narrated_through` in engine state is not
narration evidence.

Adapters should provide a stable, non-empty `source_id` (the bundled client
uses the boardgame.io match ID); once a ledger is bound to one, later states
must keep providing it. Without one, overlap fingerprints still catch
rewritten history, but a suffix beginning exactly at the next unseen index
cannot independently prove that it came from the same source. Legacy
length-only cursor marks are deliberately refused as
`legacy_cursor_unverifiable`; start a new ledger or explicitly migrate the old
one rather than guessing which events it contains.
