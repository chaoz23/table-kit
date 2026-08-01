# The instrumentation contract

What is recorded, why those things and not others, and where every default
number came from.

## One file

A session produces one append-only JSONL file. Every line is an object with
`ts` and `type`; unknown keys are allowed and preserved, so a table can add its
own context without forking the schema.

It is one file rather than two because a separate telemetry file drifts from
the play ledger the moment anything crashes, and then you are reconciling two
partial accounts of the same evening. It stays compatible with `dmcheck`'s
ledger reader because the play-lane types (`turn`, `act`, `event`) are exactly
the ones that reader recognises, and it ignores the rest.

Unknown event types are **refused at write time**. A typo that lands in the
file as an unreadable record is worse than a crash, because months later it
reads as an absence rather than an error.

Every decoded row is validated again on read. Invalid JSON, non-object rows,
unknown event types, missing fields, wrong required-field types, non-finite
numbers, and oversized lines become line-local `_malformed` diagnostics in an
unfiltered audit read. They are excluded from typed and lane reads, so corrupt
input cannot become a beat, close a pair, or change a denominator. See
[DATA_SAFETY.md](DATA_SAFETY.md) for the storage and authority boundary.
Internal state and metric consumers use the schema-valid `Ledger.records()`
view; `Ledger.read()` with no filter remains the diagnostic audit surface.

## The lanes

### `qa` — did the machinery work?

`qa.post`, `qa.post_failed`, `qa.inbound`, `qa.route`, `qa.listener`,
`qa.command`.

Delivery, latency, listener uptime, command failures. This lane exists because
the most expensive failure in a hybrid table is not a bug, it is a *silent*
bug: a listener that dies mid-session means play continues, nothing errors, and
the record simply stops.

Player prose is **not stored by default**. The kit records that a seat spoke
and roughly how much. Accumulating a transcript changes what this tool is, so
it takes an explicit `--keep-text`.

`qa.inbound` is the durable source receipt; `qa.route` is its disposition. A
valid source-native ID is committed before routing and deduplicates every
replay, including rejected and quarantined messages. Route status is one of
`routed`, `observed`, `advisory`, `ignored`, or `quarantined`, with a typed
reason retained for operator review. Unknown speakers are recorded as
`unknown`, never promoted into a plausible new seat. A message without a stable
source ID is quarantined and cannot resolve an obligation. If a later exact
config mapping repairs an unknown receipt, the new routed disposition supplies
the effective seat for derived UX metrics without rewriting the original row.

### `qc` — was the refereeing correct?

`qc.finding`, `qc.pass`, `qc.mark`.

Two severities:

- **defect** — a boundary was crossed. Pass/fail. Worth interrupting for.
- **attention** — a dosage reading. Real signal, no boundary crossed.

The checks, and what each requires as evidence:

| check | severity | fires only when |
|---|---|---|
| `undeliverable_cue` | defect | a beat cues an agent seat and lacks its literal mention |
| `seat_quiet` | defect | a seat is past the idle threshold **and** has not been checked on |
| `unnarrated` | defect | the game engine reports more log lines than were narrated |
| `roll_unconsumed` | defect | a called roll is past its TTL with no outcome |
| `unanswered` | attention | there is an inbound message **after** the last beat |
| `cue_unanswered` | attention | a cue is past its TTL |
| `long_beat` | attention | a beat exceeds the word threshold |
| `split_beat` | attention | a beat went out as more messages than the chunk limit |

Note what is *not* here: there is no check for GM silence on its own. A GM
deliberately yielding the floor and a GM who has wandered off are identical in
a log, and at professional tables the yielded floor is overwhelmingly the
common case. Accusing on silence alone would make the checker wrong most of the
time it spoke, which is how a live checker gets ignored inside twenty minutes.

`unnarrated` needs the engine's own log length. Without it the check is
**skipped, not guessed**.

### `ux` — what did the seat's evening look like?

`ux.beat`, `ux.turn`, `ux.seat_idle`.

Arithmetic on the record: who spoke, how often, how long they waited, how long
they went unaddressed. No scoring, deliberately. Seat airtime is the most
tempting number here to turn into a grade and it is a bad grade — a player can
have a superb evening in four lines, and a table where everyone speaks equally
can be one where nothing is happening. The numbers are for noticing *shapes*
worth going and looking at.

Silence is measured against the last record in the file, not the wall clock, so
a report reads the same tomorrow as it did at midnight. Live callers pass
`now=` — and the detector, the thing that actually accuses, always does.

### `uxr` — how did it feel from a seat?

`uxr.signal`, `uxr.debrief`.

The only lane that crosses the legibility boundary. Everything else here is
recoverable from the session file afterwards; none of this is.

**There is no player-facing input syntax, and that is a hard product
constraint.** An earlier version of this kit shipped six chat tokens (`!drag`
and friends). They were cut in 0.2.0. The reason is in `tablekit/uxr.py` at
length, and briefly: text RPGs handed players verb lists because 1980s parsers
could not understand a sentence, that workaround is exactly what makes them
feel like operating a computer, and an agent GM reintroducing it discards its
only advantage over a parser game. `tests/test_no_player_commands.py` keeps the
constraint from drifting back.

Six internal buckets, never surfaced to anyone at the table:

| bucket | means |
|---|---|
| `pacing` | this stretch felt slow from that seat |
| `floor` | wanted to act and the moment closed first |
| `comprehension` | lost the thread — a word, or what is happening |
| `resonance` | that landed |
| `spotlight` | got to do the thing this character is for |
| `continuity` | contradicts something already established |

Two of the six are positive on purpose. An instrument that only collects
grievances teaches a GM to avoid risk, which is a different thing from running
a good table. `spotlight` is separate from `resonance` because time spent on a
player's signature capability is not the same as time spent on scene
description, and conflating them loses the distinction that matters most to the
person holding that character.

**Three sources, with different weight:**

| source | what it is | weight |
|---|---|---|
| `dm` | the agent GM classifying a line it already read | advisory |
| `local` | an independent model pass over the same transcript | advisory, and a check on `dm` |
| `debrief` | a plain-English question asked at the close | the high-confidence channel |

The debrief is not politeness. It is structural: **a GM that does not notice
friction cannot record friction**, so inference alone inherits precisely the
blind spots this lane exists to route around. The `local` pass is the other
check — and where it and the GM disagree is itself the interesting signal. It
requires `--keep-text`, which is the honest cost of an independent read.

**The fences:**

- An inferred signal is **advisory in every path and can never become a
  defect**. Something a model thought someone meant does not get to accuse.
- Every inferred signal **stores the speaker's own words**. A classification
  with no quote is refused at write time, because nobody in the room could
  check it.
- Below `MIN_PATTERN` (3), the report lists individual moments with beat
  numbers and refuses to state a rate or a tendency. This is a specific
  correction: one player asking what a word meant was once generalised into
  "unfamiliar diction fails at this table", and their real position was the
  opposite — *"I like the new words, sometimes I will have to ask."*

### The report never claims clean for a session nobody checked

`qc` findings only reach the report if the checks were actually run. The first
real session ran them **zero times across 77 beats**, and the report printed
*"Defects — none"* — nothing was found because nothing looked, stated in the
language of a clean bill of health.

The report now distinguishes three states:

- **checked, clean** — `Defects — none`
- **never checked** — `Defects — NOT CHECKED DURING PLAY`, naming the checks
  that can only fire live (`seat_quiet`, `unnarrated`) and were never given the
  chance
- **found after the fact** — a post-hoc sweep runs at report time so there is
  always a verdict, and its findings are labelled `[found post-hoc]` because
  that is weaker evidence than a check run while the table sat there

"Was this session checked?" is answered **only** by a `qc.run` event, which
`detector.record()` is the sole writer of. The first version of this accepted
any `qc.finding` as proof — but ingest emits findings of its own
(`roll_result_advisory`), so an unchecked session had findings in it and went
straight back to claiming clean. Inferring "someone examined this" from a side
effect of something else is the same false negative one level down.

The sweep deliberately **excludes the live-only checks**. It could technically
fire `seat_quiet` when the last beat happens to be recent, but doing so would
contradict the line printed directly above it. A sweep that claims to have done
what the report says it could not do is worse than one that admits the gap.

### `out` — did it work?

`out.open` / `out.close`, paired by id.

A transcript records *decisions*. It shows that a GM cued a seat, called for a
roll, checked on someone quiet. It does not show whether any of it worked,
because the payoff is separated from the move by minutes and other people's
turns. A pair records both halves as one thing.

| pair | opens when | permitted outcomes |
|---|---|---|
| `cue` | a beat addresses a seat | `taken`, `ignored`, `expired`, `superseded` |
| `roll` | a roll is called for | `consumed`, `unconsumed`, `superseded` |
| `checkin` | a quiet seat is checked on | `returned`, `absent`, `superseded` |
| `endmarker` | a session ends | `matched`, `diverged`, `superseded` |

Roll pairs carry `dc` and `rolled_by`. **Recording a DC is not stating one** —
difficulty is conveyed in the fiction by default, and the number is said out
loud only when it is a reachable long shot you were otherwise going to refuse.
But it is recorded every time, because a known DC is what converts fail-forward
and several other craft rules from unfalsifiable to testable. `rolled_by`
captures the seat's own preference (`self` by default; `dm` for the minority who
would rather not roll), so "did this land differently for seats that do not roll
their own dice" stays an answerable question.

**Resolution is one typed transition, never a global sweep.** One inbound or
roll result may close at most one open pair. A supplied correlation ID must name
an open obligation of the expected kind and seat. Without one, no compatible
pair is closed, even if only one is open; the event is durably receipted and
quarantined as `missing_correlation` with the compatible pair IDs retained for
diagnosis. Empty messages acknowledge nothing.
Duplicate IDs, orphan or reordered closes, kind changes, invalid kind-specific
outcomes, second closes, and closes timestamped before their opens are explicit
lifecycle failures, not state that gets folded into a plausible answer.

Generated pair IDs use a timestamp for diagnostics plus a UUID4; the ledger
still atomically refuses duplicate opens. This removes the former short random
suffix as a correctness dependency.

**Free-form roll prose is advisory.** `14`, `nat 20`, and explicit arithmetic
may create one `roll_result_advisory` attention item for correlation, but they
never create an `act` or close a roll. Damage, healing, hit points, movement,
and other non-roll quantities are ignored by this detector. A configured relay
may resolve a roll only with verified bot role, exact sheet-ID or exact
normalized alias attribution, a numeric total, any exposed natural die in
range, and explicit correlation to the open roll pair. Missing or impossible
relay evidence is quarantined with no `act` and no resolution; valid but
uncorrelated results retain the observed `act` and are quarantined without
closing an obligation. A successful roll `out.close` is self-contained: its
obligation ID, source key, total, exposed die/natural, confidence, and provenance
travel with the transition.

Source-native principal IDs and evidence are retained, but alias fallback is
still local and non-authoritative. Host-owned identity and the suite-wide
`TableEvent` envelope remain portfolio decisions in `PORT-002`/`PORT-003`.

**The bar for being a pair:** the payoff must be observable by someone other
than the person who made the move. "Did the scene feel tense" is not a pair.
"Did the seat act within five minutes" is. Anything failing that test belongs
in `uxr`, where it is honestly labelled self-report, or nowhere.

**Expiry is a real outcome.** A cue nobody answered and a roll nobody consumed
both look like silence in the file. `sweep()` converts them to named outcomes
explicitly, so absence gets counted instead of quietly shrinking the
denominator.

**Rates are withheld below three closed pairs.** "0% cue uptake" computed from
one cue is a finding about the sample, not about the table.

## Where the defaults came from

| default | value | provenance |
|---|---|---|
| `seat_quiet_s` | 600 | **live failure.** A human seat went most of an hour unaddressed while agent seats carried a scene to its climax. Nothing in the record looked wrong. |
| `long_beat_words` | 120 | **measured, loosely.** Professional GM median is 8–14 words with about one line in six running long; 120 is well past the tail, so it flags only beats that had better be paying something off. |
| `max_chunks` | 2 | **transport reasoning.** A beat arriving as three messages is read as three beats and the table answers the first. |
| `cue_ttl_s`, `roll_ttl_s` | 300 | **judgment, not measurement.** Labelled as such; retune on your own outcome pairs after a few sessions, which is what the `out` lane is for. |
| signal floor | 3 | **the gorse correction** (above). |
| `MIN_BEATS` for a report | 3 | a session shorter than this has nothing to report on, and "no defects found" about it would be a lie of omission. |

The honest summary: one default is from live failure, one is loosely
corpus-anchored, and the rest are reasoned defaults waiting for your data. They
are all in `table.json` and they are all meant to be changed.

## What this cannot tell you

Whether a beat landed. Whether an NPC felt authentic. Whether your players are
tiring. Whether the story was any good.

The `uxr` lane gets closer to these than anything derivable from a transcript,
but it is self-report from a handful of people, and it is labelled that way
everywhere it appears. When you want to know what a player prefers: **ask
them, do not model them.**
