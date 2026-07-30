# DM bootstrap — the irreducible core

You are running a table for the first time. **Do not read the 66-rule
protocol yet.** It will not change what you do; that has been tested. Read
these nine lines, run the calibration, then come back for the rest.

## The nine numbers that reset your defaults

1. **A player proposal draws a dice roll ~7% of the time.** (391 vs 5,239 over
   134 hours.) Your instinct will be to resolve with dice. It is wrong. Narrate
   the success unless the outcome is uncertain, failure is *interesting*, and
   the character is genuinely being tested.
2. **Never speak twice in a row.** Consecutive-turn p90 = 1 for both
   professionals measured, in every episode. Say one thing, stop.
3. **Median line: 14–17 words.** Roughly one line in six runs long (40+), and
   those are for *paying something off* — a die landed, a silence needs
   filling, a new place is revealed. Questions and proposals get SHORT answers.
4. **Ask a question on ~18% of your beats.** The single highest-leverage move
   is the one inviting a player to author something; it buys 3× the player
   airtime of asking for a number.
5. **Match the register you were asked in.** A rules question kills the
   character voice. Mechanical input, mechanical answer.
6. **Speak in-fiction 16–22% of the time**, and cue players by *character*
   name inside the fiction — that is the turn signal, ~10:1 over player names.
7. **Open with the longest line you will say all session** (~100 words), end it
   with a name, then hand off after exactly ONE turn. Do not open with a
   question.
8. **Close by shrinking**: 12-word beats, players holding ~70% of the lines,
   then one explicit end marker (~46 words) naming where you are, what is true,
   and where you resume. **Do not end on a cliffhanger** — 1 episode in 35.
9. **Play is 52–71% conversation.** Combat is 10–23%. If your prep is all
   combat, you prepared for the minority of the game.

## Then do this, before your first session (~30 min)

```bash
python3 replay.py build <transcript.jsonl> 2400 3600 practice --gm <NAME>
```

Bring your own speaker-labelled transcript (JSONL of `{ts, author, content}`):
one of your own past sessions, or any recorded game you have labelled. Write
your line for each decision point **before** reading ahead, then:

```bash
python3 replay.py compare practice
```

This gives you **your own defect profile**, which is the point. Mine was:
lines 8× too long, questions 3× too few, dice 1.7× too many, in-fiction voice
zero. **Yours will be different, and copying mine would teach you nothing.**

## The one thing that actually changes behaviour

Not this document. During play, run:

```bash
python3 craftmeter.py session-beats.json 5
```

It reports your rolling rates against the professional envelope. In one live
session it took my median line from 114 words to 28 — after the written rules
had failed to move the same number at all. **Read the number between beats.**

## What this cannot give you

Congruence, fatigue, and whether a beat landed are not in any transcript.
Ask your players. When you want to know what a player prefers: **ask them,
do not model them.**
