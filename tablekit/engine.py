"""The ledger tap — mirror a game engine's log into the session file.

`engine/` ships a small boardgame.io server that owns combat state: initiative
order, hit points, seeded replayable dice, and an append-only log. That log is
authoritative about what *happened*; the session file needs to know about it
for two reasons.

  * A conduct checker reads `turn` / `act` / `event` records to notice things
    like an engine event the table was never told about. It cannot read
    boardgame.io's format.
  * "The engine says the ward expired four rounds ago" is exactly the class of
    fact a GM loses track of, and the whole point of writing it down.

The tap is deliberately one-directional and idempotent. It reads engine state,
writes the log lines it has not already written, and records how far it got in
the session file itself rather than in a sidecar. A tap that keeps its position
in a separate file loses its place the first time a session is resumed from a
different directory, and then silently re-narrates twenty minutes of combat.
"""

import json
import subprocess


def synced_through(ledger):
    """How many engine log lines are already in the session file."""
    marks = ledger.read(etype="qc.mark")
    return max((m.get("engine_log_len", 0) for m in marks), default=0)


def tap(ledger, state):
    """Mirror new engine log lines. `state` is the engine's state dict.

    Returns the events written. Safe to call after every move.
    """
    log = state.get("log") or state.get("log_tail") or []
    total = state.get("log_len", len(log))
    have = synced_through(ledger)
    if total <= have:
        return []
    new = log[-(total - have):] if len(log) >= (total - have) else log
    written = []
    for line in new:
        text = line if isinstance(line, str) else json.dumps(line)
        etype = "event" if "[ruling" in text else "act"
        actor = (text.split("] ", 1)[-1].split("→")[0].split(":")[0]
                 .replace("[ruling.", "").strip())
        written.append(ledger.append(etype, actor=actor or "engine", text=text))
    turn = state.get("turn")
    units = state.get("units") or {}
    if turn and units.get(turn):
        name = str(units[turn]).split(" [")[0]
        written.append(ledger.append("turn", actor=name))
    ledger.append("qc.mark", narrated_through=have, engine_log_len=total)
    return written


def state_from(cmd):
    """Run a command that prints engine state as JSON and parse it.

    Used to keep the engine a subprocess rather than a dependency: the kit
    works with any engine that can print its state.
    """
    out = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                         text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"engine state command failed: {out.stderr.strip()}")
    return json.loads(out.stdout)
