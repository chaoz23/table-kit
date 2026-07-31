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

## 2. One cursor owner

The listener owns the watch cursor. Nothing else writes it.

Two writers to one cursor file will eventually acknowledge past an unread
message, and the symptom — a player's turn that everyone swears they sent —
takes an evening to diagnose because the message is right there in the channel
when you go looking.

If you need a second reader, give it its own cursor file.

## 3. The relay tax

Every hop between a player and the game costs latency, and latency at a table
is not a technical metric — it is the difference between a scene with momentum
and a scene where everyone is waiting. `qa.post` records the round trip so the
report can show what the plumbing actually cost.

Two practical consequences:

- **Batch nothing that is time-sensitive.** A beat held back to bundle with the
  next one arrives after the moment it was for.
- **A dead listener is worse than a slow one.** Play continues, nothing errors,
  and the record stops. `listen.mjs` reconnects unconditionally with backoff
  and logs every interruption; the report surfaces the count.

## 4. Splitting reshapes play

Chat platforms cap message length. A beat that arrives as three messages is
read as three beats, and the table answers the first one — a craft defect
introduced purely by transport.

The splitter breaks on paragraph boundaries, then lines, and never mid-word.
Every split is recorded with its chunk count, and more than two is an attention
item, because at that point the transport is editing your pacing for you.

## 5. Ingestion is idempotent, and it has to be

Push listeners deliver each message once. **Polling transports do not** — they
re-read a window, and a GM checking the channel twice between beats is the
normal case, not an edge case.

So `ingest_message` skips any message whose platform `id` it has already
recorded. Without that, every re-read doubles a seat's line count, and the
participation numbers the report is built on quietly become fiction — the worst
kind of failure here, because the output still looks perfectly reasonable.

A transport that cannot supply stable ids gets at-least-once delivery rather
than silent dropping. Over-counting you can see; under-counting you cannot.

## 6. Roll relays post as themselves

A player clicking a skill on their D&D Beyond sheet with Beyond20 installed
gets the roll posted to Discord — by **the Beyond20 bot, in an embed**, not by
the player and not in message content.

Filed naively that breaks three things at once: every human's dice land under
one synthetic seat, no roll pair ever closes, and a player who rolled all
evening reads as silent in the participation numbers.

So `transport.roll_relay_bots` lists the relays, the listener forwards embeds,
and ingestion attributes a relayed roll to whichever seat's name appears in it.
**An unattributable relay is recorded as a failure, never guessed at** — a roll
credited to the wrong seat is worse than one credited to none.

This is also why the seat panel we scoped is unnecessary for D&D Beyond
tables: Beyond20 already provides the click-a-skill affordance, so the player
never types dice syntax, which is the whole point of rule 4.

**But never assume the relay is there.** The same table will not have it every
night — somebody joins from a phone, an extension is not installed, a browser
is signed out. So a roll arriving as ordinary text is a supported case, not a
fallback: while a roll is outstanding for a seat, `detect_typed_roll` reads
"14", "nat 20", "18 + 3 = 21" and consumes it.

The gate is deliberately conservative, and when it is unsure it **asks**:
an ambiguous number produces a `roll_needs_confirming` attention item naming
what it thinks it saw, and the pair stays open. A wrong total silently consumed
corrupts the ledger and nobody notices until the arithmetic stops making sense.

The report shows `how rolls arrived` (relay / typed / dm), so which pattern
actually held on a given night is a recorded fact rather than an assumption.

## 7. Seat sync

The kit resolves a chat display name to a seat id through `aliases`. Keep that
list generous: people rename themselves mid-campaign, and an unresolved speaker
silently becomes their own seat in the record, which quietly halves the
apparent participation of the real one.

The GM's own posts arrive back through the listener. They are dropped on
ingest — recording them again would double every beat and halve every apparent
player share.
