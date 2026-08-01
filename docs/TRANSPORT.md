# Transport — the things that cost an evening to learn

Everything here is a lesson from a live table, written down so the next person
does not have to buy it again.

## 1. Mandatory mentions

Agent chat bridges commonly gate bot-to-bot delivery behind "only if
mentioned". So a cue that addresses an agent seat by name — the in-fiction cue
that works perfectly for every human at the table — is accepted by the API,
appears in the channel, and is **never delivered**.

Nothing errors. The seat goes quiet, and the GM concludes the agent is
thinking.

This is the only defect in the system with no symptom at run time, so it is
handled three times over:

1. `config.load()` **refuses** a config with an agent seat that has no mention.
2. `post.post()` **repairs** it — the mention is prepended before the message
   leaves, and the repair is recorded so "how often does the GM forget"
   remains an answerable question.
3. `detector.check()` **reports** it for every cued beat in the session, not
   just the most recent one — an undeliverable cue from twenty minutes ago is
   usually the *reason* a seat has gone quiet, and reporting the silence
   without the cause sends you looking in the wrong place.

## 2. Message content is a capability boundary

Discord's `MESSAGE_CONTENT` intent controls message content fields in both
Gateway events **and HTTP message responses**. Fetching the same message over
REST cannot recover content that the application is not allowed to receive.
The listener therefore requests `GUILD_MESSAGES | MESSAGE_CONTENT` and fails
closed if Discord rejects or cannot prove that capability. It never drops to
"basic intents + REST" and calls the resulting partial stream complete.

The bot's own user ID is discovered from `READY.user.id`. A configured bot ID
is not trusted: stale identity would either ingest the bot's own posts or drop
another account's messages. `READY.application.flags` is the evidence for
message-content capability. Authentication, API/shard, and intent close codes
(`4004`, `4010`–`4014`) stop with a typed diagnostic and exit status 2 rather
than entering a reconnect loop.

Discord documents these behaviors in its [Gateway guide][gateway],
[application flags][application-flags], [message resource][messages], and
[Gateway close-code table][close-codes].

## 3. Resume is not a durable checkpoint

Within one running process, the listener retains `session_id`,
`resume_gateway_url`, and the last received Gateway sequence from `READY`.
Recoverable disconnects attempt opcode 6 Resume; a successful replay ends in
`RESUMED`. Heartbeats use the server's interval, answer server opcode 1
immediately, and terminate a zombied connection when an ACK is missing between
scheduled heartbeats.

That is connection recovery, not durable capture. The listener cannot know
when a line written to stdout has committed to the ledger, so it writes **no
watch cursor**. A process restart starts a new identified session. This slice
does not backfill channel history and cannot rule out an initial or crash-window
gap. `DiscordGatewayCapture.health()` states that boundary as
`mode: live_uncheckpointed`, `durable_checkpoint: false`,
`cold_start_backfill: false`, `gap_detection: not_implemented`,
`known_gap_count: null`, and `authoritative: false`. Last Gateway and target
message observations are explicitly `storage: memory_only`, with current
staleness calculated from the injected clock. Structured connection diagnostics
go to stderr while stdout remains message-only.

The process spaces repeat Identify attempts by at least five seconds and
reports their count. It does not yet query Discord's authenticated Get Gateway
Bot endpoint, so `health().identify.session_start_limit.status` is
`not_queried`; remaining daily starts, reset time, and shard concurrency are
unknown rather than invented.

Do not describe this listener as complete or durable until ingestion can
acknowledge a committed native message ID/sequence and the transport can record
explicit gaps. Persisting a receive cursor before that acknowledgment would
recreate the exact loss window a cursor is supposed to prevent.

## 4. The relay tax

Every hop between a player and the game costs latency, and latency at a table
is not a technical metric — it is the difference between a scene with momentum
and a scene where everyone is waiting. `qa.post` records the round trip so the
report can show what the plumbing actually cost.

Two practical consequences:

- **Batch nothing that is time-sensitive.** A beat held back to bundle with the
  next one arrives after the moment it was for.
- **A dead listener is worse than a slow one.** Play continues, nothing errors,
  and the record stops. `listen.mjs` attempts Resume for recoverable states,
  starts a fresh Identify only when required, and stops on fatal close codes.
  Each transition is a structured stderr diagnostic; ledger/report health is
  deferred until the downstream writer can commit it safely.

## 5. Splitting reshapes play

Chat platforms cap message length. A beat that arrives as three messages is
read as three beats, and the table answers the first one — a craft defect
introduced purely by transport.

The splitter breaks on paragraph boundaries, then lines, and never mid-word.
Every split is recorded with its chunk count, and more than two is an attention
item, because at that point the transport is editing your pacing for you.

## 6. Ingestion is idempotent, and it has to be

Gateway replay and polling can both deliver a message more than once. Polling
transports deliberately re-read a window, and a GM checking the channel twice
between beats is the normal case, not an edge case.

So `ingest_message` commits a `qa.inbound` receipt keyed by transport kind and
platform ID **before** any routing effect. The receipt deduplicates successful,
rejected, and quarantined messages alike; acts, findings, and resolutions also
carry the same source key so a replay can safely repair a crash window without
duplicating effects. `qa.route` records whether that receipt was routed,
observed, advisory, ignored, or quarantined and why. A receipt also retains a
source fingerprint rather than the raw payload; the same native ID replayed
with different evidence is quarantined instead of acquiring a second meaning.

A transport that cannot supply a stable ID is recorded and visibly
quarantined. It cannot resolve an obligation: at-least-once text with no
identity is not safe evidence for a state transition.

## 7. Roll relays post as themselves

A player clicking a skill on their D&D Beyond sheet with Beyond20 installed
gets the roll posted to Discord — by **the Beyond20 bot, in an embed**, not by
the player and not in message content.

Filed naively that breaks three things at once: every human's dice land under
one synthetic seat, no roll pair ever closes, and a player who rolled all
evening reads as silent in the participation numbers.

So `transport.roll_relay_bots` lists the relays, the listener forwards embeds,
and ingestion requires all of the following before a relay can act: the
configured display name, `is_bot: true`, a numeric total, and attribution by an
exact source-native sheet ID or an exact normalized configured alias in a
structured embed field. Display names are not authority, and names are never
searched as substrings (`Will` does not capture `William`). **An unattributable,
empty, spoofed, or naturally impossible relay is quarantined, never guessed
at**, and produces neither an `act` nor a pair resolution.

This is also why the seat panel we scoped is unnecessary for D&D Beyond
tables: Beyond20 already provides the click-a-skill affordance, so the player
never types dice syntax, which is the whole point of rule 4.

**But never assume the relay is there.** The same table will not have it every
night — somebody joins from a phone, an extension is not installed, a browser
is signed out. Ordinary text such as "14", "nat 20", or "18 + 3 = 21" is
therefore detected, but it is **advisory only**. It may create one
`roll_result_advisory` attention item asking the operator to correlate and
confirm; it never creates an `act` or consumes a roll. Damage, healing, hit
points, movement, and other non-roll quantities are excluded. A wrong total
silently consumed corrupts the ledger and nobody notices until the arithmetic
stops making sense.

The report shows observed relay routes, routing advisories, and unresolved
quarantine counts/reasons; repaired quarantine events remain a separate history
count. Which path actually held on a given night is therefore a recorded fact
rather than an assumption.

## 8. Seat sync

The repo-local fallback resolves a chat display name only by exact ID, display,
or configured alias after Unicode compatibility normalization, case folding,
and whitespace folding. Duplicate normalized identities are refused at config
load. Keep aliases current, but do not make them fuzzy: an unresolved speaker
is retained as `unknown` and quarantined, never invented as a new seat.

The source-native principal ID and role evidence are preserved on every
receipt. They are not yet an authoritative host-owned identity mapping; that
suite-wide contract belongs to `PORT-002`/`PORT-003`.

The GM's own posts arrive back through the listener. They receive the same
durable, deduplicated receipt and an `ignored: gm_echo` disposition, but do not
become a second beat or player line.

## 9. One result, one obligation

An inbound/result resolves at most one compatible pair. An explicit `pair_id`
must refer to an open obligation of the expected kind and seat. Without one,
there must be exactly one compatible pair; multiple candidates are quarantined
as `ambiguous_correlation`, and none is closed. Blank content acknowledges
nothing. This is intentionally narrower than guessing based on timing: temporal
proximity is useful evidence for a human, not enough authority for an automated
ledger transition.

[gateway]: https://docs.discord.com/developers/events/gateway
[application-flags]: https://docs.discord.com/developers/resources/application#application-object-application-flags
[messages]: https://docs.discord.com/developers/resources/message
[close-codes]: https://docs.discord.com/developers/topics/opcodes-and-status-codes#gateway-gateway-close-event-codes
