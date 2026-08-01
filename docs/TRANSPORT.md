# Transport — the things that cost an evening to learn

Everything here is a lesson from a live table, written down so the next person
does not have to buy it again.

## 1. Outbound writes are gated and transactional

`post.post()` does no ledger or network I/O unless
`transport.write_enabled` is the JSON boolean `true`. The generated example
sets it to `false`. Enabling it is explicit permission to write to the
configured channel; it is not evidence that the rest of a deployment is
production-ready.

For every enabled logical post, the ledger records a small saga:

1. `qa.post.prepare` fixes an immutable operation ID, complete bounded source
   text/payload plan, content/plan digests, and intended cue before the first
   network call.
2. Each Discord success is immediately fsync'd as `qa.post.receipt`, with its
   operation ID, chunk index, message ID, and nonce.
3. Only after every receipt exists do the play beat, cue obligation,
   compatibility `qa.post`, and final `qa.post.commit` become durable.

A failure after at least one receipt is `qa.post.partial`, never a full beat
and never an invisible failure. Resume with the exact same `operation_id`,
text, cue, and kind. A changed plan is refused. Receipted chunks are never sent
again. After a call ends or crashes, a resumed unreceipted chunk is sent only
after bounded remote history covers the prepare timestamp; matching is by
nonce, exact content, and the configured bot identity. Missing coverage,
conflicting receipts, duplicate remote matches, or changed content all fail
closed. Immediate bounded retries inside one call use Discord's enforced nonce,
as described below.

Calls for the same ledger and operation ID also take a bounded local saga lock
before reading or changing that state. The lock is a private sibling file;
the operation ID selects a byte range, so unrelated posts can still progress.
It is released by the kernel on process exit, including a hard crash. A caller
that cannot acquire it within five seconds returns `operation_busy` instead of
stalling the table. Cross-process outbound posting therefore requires POSIX
byte-range locks and a local filesystem; on a platform without them, writes
fail closed. This serializes one outbound saga, not every other kind of ledger
workflow.

`post.resume(cfg, ledger, operation_id, ...)` reconstructs the call from the
durable prepare plan, so recovery does not depend on an interrupted caller
still holding a long prompt in memory. The complete outbound plan is sensitive
table data; see `DATA_SAFETY.md`.

Callers should create and retain the operation ID before calling. An omitted ID
is generated and written to the prepare record, but after a process crash the
caller then has to discover it in the ledger before calling `resume`.

Discord's nonce protection is explicitly short-lived (the API describes a
window of only a few minutes). Ambiguous timeout and 5xx retries therefore use
the same deterministic nonce immediately with `enforce_nonce=true`; recovery
after a longer outage depends on remote-history reconciliation, not on nonce
retention. Because Discord checks uniqueness by author rather than channel, the
deterministic nonce also includes the configured bot and channel identity; the
same human-readable operation ID used at two tables cannot suppress one table's
message. A 429 honors Discord's `retry_after`, bounded jitter, attempt cap, and
one retry-time budget shared by the entire logical post. The transport never
hard-codes Discord bucket rates.

The built-in adapter validates the successful response's numeric message ID,
echoed nonce, exact content, configured bot author, and destination channel
before writing a receipt. A malformed success is delivery-uncertain and is not
blindly retried. Recovery history must contain canonical snowflake IDs and
timezone-qualified timestamps in strict newest-to-oldest order, without
pagination repeats; each timestamp must also agree with the time encoded in its
[Discord snowflake](https://docs.discord.com/developers/reference#snowflakes).
Only the oldest validated message on a page can prove that history reached the
prepare time. Malformed coverage evidence is a refusal, never permission to
resend.

The built-in live sender also requires `channel_id`, `bot_user_id`, and the
configured token environment variable before preparing anything. A custom
`send_fn`/`history_fn` is an adapter trust boundary: it must preserve the same
payload, authenticated-author, stable-ID, history-coverage, and durability
semantics. `send_fn` must return a message object containing a non-empty `id`
and the exact requested `nonce`; a bare ID string is not a durable receipt.

Keep live writes disabled until authenticated/durable ingestion, suite consent
and provenance decisions, and live Discord timeout/rate-limit/crash exercises
are complete. Unit tests prove deterministic behavior at the seams; they do
not prove Discord permissions, gateway continuity, or production recovery.

## 2. Mandatory mentions

Agent chat bridges commonly gate bot-to-bot delivery behind "only if
mentioned". So a cue that addresses an agent seat by name — the in-fiction cue
that works perfectly for every human at the table — is accepted by the API,
appears in the channel, and is **never delivered**.

Nothing errors. The seat goes quiet, and the GM concludes the agent is
thinking.

This is the only defect in the system with no symptom at run time, so it is
handled three times over:

1. `config.load()` **refuses** a config with an agent seat that has no mention.
2. `post.post()` validates Discord's numeric user-mention form and **repairs**
   the content to contain exactly one target mention. The fiction still leads;
   the mention trails the first chunk so the target is notified before later
   chunks arrive. The repair is recorded so "how often does the GM forget"
   remains an answerable question.
3. `detector.check()` **reports** it for every cued beat in the session, not
   just the most recent one — an undeliverable cue from twenty minutes ago is
   usually the *reason* a seat has gone quiet, and reporting the silence
   without the cause sends you looking in the wrong place.

Discord `allowed_mentions` is deny-by-default. `@everyone`, `@here`, roles,
and non-target user mentions remain visible text but cannot ping. Only the
configured target user is allowlisted, and only on the first chunk.

This follows Discord's current [Create Message and Allowed Mentions
contract](https://docs.discord.com/developers/resources/message#create-message):
ordinary messages otherwise parse mentions by default, explicit `users` and a
`parse` entry for users cannot be combined, `nonce` is at most 25 characters,
and `enforce_nonce=true` asks Discord to return the recent matching message
rather than create another one. Rate-limit delays come from Discord's current
[rate-limit response contract](https://docs.discord.com/developers/topics/rate-limits),
not hard-coded bucket assumptions.

## 3. Message content is a capability boundary

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

## 4. Resume is not a durable checkpoint

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

## 5. The relay tax

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

## 6. Splitting reshapes play

Chat platforms cap message length. A beat that arrives as three messages is
read as three beats, and the table answers the first one — a craft defect
introduced purely by transport.

Discord documents a 2,000-character content limit but does not define that
word as a Unicode measurement unit. The splitter therefore measures
conservatively in UTF-16 code units. It prefers paragraph, line, and whitespace
boundaries; keeps combining, emoji/ZWJ, regional-flag, Indic-conjunct, Hangul,
Prepend, and format-mark grapheme sequences together; and hard-splits an
unbroken token only between those clusters. A single pathological cluster
larger than the platform limit is refused. The target mention is included
inside the first chunk's budget.

Every split is recorded with its chunk count, and more than two is an attention
item, because at that point the transport is editing your pacing for you. One
logical operation is hard-capped at ten Discord messages so an agent mistake
cannot turn one call into an unbounded channel write.

## 7. Ingestion is idempotent, and it has to be

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

## 8. Roll relays post as themselves

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

## 9. Seat sync

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

## 10. One result, one obligation

An inbound/result resolves at most one compatible pair. An explicit `pair_id`
must refer to an open obligation of the expected kind and seat. Without one, no
compatible pair is closed, even when only one is open; the inbound/result is
durably receipted and quarantined as `missing_correlation`, with candidate pair
IDs retained for diagnosis. Blank content acknowledges nothing. This is
intentionally narrower than guessing based on timing or uniqueness: both are
useful evidence for a human, not enough authority for an automated ledger
transition. It remains the interim contract until PORT-002/PORT-003 supply
host-owned correlation and identity.

[gateway]: https://docs.discord.com/developers/events/gateway
[application-flags]: https://docs.discord.com/developers/resources/application#application-object-application-flags
[messages]: https://docs.discord.com/developers/resources/message
[close-codes]: https://docs.discord.com/developers/topics/opcodes-and-status-codes#gateway-gateway-close-event-codes
