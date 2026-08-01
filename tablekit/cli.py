"""Command line. Hand-rolled dispatch, because argparse subcommands cost more
than they are worth for a flat verb list.

Design constraint that shaped everything here: **one command per beat, and it
must not slow the table down.** An instrument that takes three commands and a
context switch to record a moment will be used for the first twenty minutes of
the first session and never again. So `beat` does the whole job — records the
beat, opens the cue pair, checks the cue is deliverable, and prints anything
worth knowing — and everything else is either automatic or a single token a
player types in chat.

Exit codes, per the family convention:
  0  clean / done
  1  findings — something to look at
  2  refused — the tool will not answer (bad input, not enough data)
"""

import json
import math
import os
import sys
import time

from . import detector, pairs, report, ux, uxr
from .config import ConfigError, load as load_config
from .events import (SCHEMA, Ledger, PAIR_KINDS, PAIR_OUTCOMES,
                     ROUTE_STATUSES, SchemaError)

__version__ = "0.5.1"

USAGE = """tablekit — instrumentation for a live hybrid table

  tablekit init [path]              write a starter table.json

  during play
    tablekit beat "<text>" [--cue SEAT] [--chunks N] [--kind scene|combat|ooc]
    tablekit inbound --seat S --text "<what they said>" [--pair ID]
    tablekit roll --seat S "<what for>" [--dc N]   called for a roll
    tablekit consumed <pair-id> [--outcome consumed]
    tablekit checkin --seat S                  checked on a quiet seat
    tablekit turn --seat S [--wait N]          a seat got the floor
    tablekit signal --seat S --kind pacing --quote "<their words>"
                                               record an inferred signal
    tablekit park "<what looked off>" [--topic X]   park it, keep playing
    tablekit park --list | --done "<detail>"    the standing issues list
    tablekit qc [--state FILE] [--record]      run the checks
    tablekit pairs                             what is still open
    tablekit sweep                             expire what has timed out

  after play
    tablekit debrief                  the plain-English questions to ask
    tablekit debrief --seat S --q "..." --a "..."      record an answer
    tablekit report [--json] [--out FILE]
    tablekit schema                   the event schema, as JSON

  options
    --config PATH   table config (default $TABLE_CONFIG or ./table.json)
    --session NAME  session name -> <data_dir>/<name>.jsonl
    --ledger PATH   use this file directly, ignore config paths
    --version
"""


def _flag(args, name, default=None, takes_value=True):
    positions = [i for i, value in enumerate(args) if value == name]
    if not positions:
        return default
    if len(positions) > 1:
        raise ConfigError(f"{name}: flag may be supplied only once")
    i = positions[0]
    args.pop(i)
    if not takes_value:
        return True
    if i >= len(args) or args[i].startswith("--"):
        raise ConfigError(f"{name}: expected a value")
    return args.pop(i)


def _unknown_options(args):
    unknown = [value for value in args if value.startswith("-")]
    if unknown:
        raise ConfigError(
            f"unknown option(s): {', '.join(unknown)}; see `tablekit --help`")


def _no_positionals(args, command):
    _unknown_options(args)
    if args:
        raise ConfigError(
            f"{command}: unexpected positional argument(s): {' '.join(args)}")


def _text_argument(args, command, flagged=None, required=False, default=None):
    _unknown_options(args)
    if flagged is not None and args:
        raise ConfigError(
            f"{command}: provide text either positionally or by flag, not both")
    if len(args) > 1:
        raise ConfigError(f"{command}: expected one quoted text argument")
    value = flagged if flagged is not None else (args[0] if args else default)
    if required and (not isinstance(value, str) or not value.strip()):
        raise ConfigError(f"{command}: expected non-empty text")
    return value


def _integer(value, flag, minimum=None, maximum=None):
    if isinstance(value, bool):
        raise ConfigError(f"{flag}: expected an integer")
    try:
        # int("1.0") is deliberately refused; accepting it differs by caller.
        parsed = int(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{flag}: expected an integer") from e
    if isinstance(value, str) and str(parsed) != value.strip().lstrip("+"):
        raise ConfigError(f"{flag}: expected an integer")
    if minimum is not None and parsed < minimum:
        raise ConfigError(f"{flag}: must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise ConfigError(f"{flag}: must be at most {maximum}")
    return parsed


def _finite_number(value, flag, minimum=None, maximum=None):
    if isinstance(value, bool):
        raise ConfigError(f"{flag}: expected a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{flag}: expected a finite number") from e
    if not math.isfinite(parsed):
        raise ConfigError(f"{flag}: expected a finite number")
    if minimum is not None and parsed < minimum:
        raise ConfigError(f"{flag}: must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise ConfigError(f"{flag}: must be at most {maximum}")
    return parsed


def _ctx(args):
    """Resolve (config, ledger). Config is optional; a ledger is not."""
    cfg_path = _flag(args, "--config")
    session = _flag(args, "--session")
    ledger_path = _flag(args, "--ledger")
    if cfg_path is not None and not cfg_path.strip():
        raise ConfigError("--config: expected a non-empty path")
    if session is not None and not session.strip():
        raise ConfigError("--session: expected a non-empty ID")
    if ledger_path is not None and not ledger_path.strip():
        raise ConfigError("--ledger: expected a non-empty path")
    cfg = None
    env_cfg = os.environ.get("TABLE_CONFIG")
    if cfg_path is not None or env_cfg:
        cfg = load_config(cfg_path or env_cfg)
    elif os.path.lexists("table.json"):
        # An implicit config is optional only when absent. If present but bad,
        # it is authoritative input and must fail before a fallback write.
        cfg = load_config("table.json")
    if session is not None and ledger_path is not None:
        raise ConfigError("--session and --ledger are mutually exclusive")
    if session is not None and not cfg:
        raise ConfigError("--session requires a table config with data_dir")
    if ledger_path is None:
        if cfg:
            ledger_path = cfg.ledger_path(session)
        else:
            ledger_path = os.environ.get("TABLE_LEDGER", "./table-data/session.jsonl")
    _unknown_options(args)
    return cfg, Ledger(ledger_path)


def _known_seat(cfg, name, flag="--seat"):
    if not cfg or not name:
        return None
    seat = cfg.seat(name)
    if not seat:
        known = ", ".join(s.id for s in cfg.seats)
        raise ConfigError(f"{flag}: unknown seat {name!r}; known seats: {known}")
    return seat


def _seat_id(cfg, name):
    if not cfg or not name:
        return name
    s = cfg.seat(name)
    return s.id if s else name


def cmd_init(args):
    _unknown_options(args)
    if len(args) > 1:
        raise ConfigError("init: expected at most one config path")
    path = args[0] if args else "table.json"
    example = os.path.join(os.path.dirname(__file__), "table.example.json")
    with open(example) as f:
        text = f.read()
    try:
        with open(path, "x") as f:
            f.write(text)
    except FileExistsError:
        print(f"{path} already exists — not overwriting", file=sys.stderr)
        return 2
    print(f"wrote {path}")
    print("Edit it: seats, and a literal 'mention' for every agent seat.")
    return 0


def cmd_signal(args):
    """Record a signal inferred from what a player already said.

    There is deliberately no player-facing counterpart to this command.
    Signals are classified in the background from ordinary speech; the table
    is never asked to learn a syntax. See tablekit.uxr.
    """
    seat = _flag(args, "--seat")
    kind = _flag(args, "--kind")
    quote_flag = _flag(args, "--quote")
    source = _flag(args, "--source", "dm")
    note = _flag(args, "--note")
    if not seat or not kind:
        print("signal: --seat and --kind are required "
              f"(kinds: {', '.join(sorted(uxr.SIGNALS))})", file=sys.stderr)
        return 2
    cfg, led = _ctx(args)
    # _ctx strips the global flags, so anything left is a real positional.
    quote = _text_argument(args, "signal", quote_flag, required=True)
    _known_seat(cfg, seat)
    try:
        rec = uxr.record_signal(led, _seat_id(cfg, seat), kind, quote,
                                source=source, note=note)
    except uxr.SignalError as e:
        print(f"signal: {e}", file=sys.stderr)
        return 2
    print(f"{kind} signal recorded for {rec['seat']} at beat {rec['beat']} "
          f"(source: {source})")
    return 0


def cmd_debrief(args):
    """Ask, or record, the session-close questions.

    With no arguments this prints the questions in plain English — they are
    meant to be asked conversationally, not pasted as a form.
    """
    seat = _flag(args, "--seat")
    q = _flag(args, "--q")
    a = _flag(args, "--a")
    kind = _flag(args, "--kind")
    cfg, led = _ctx(args)
    _no_positionals(args, "debrief")
    supplied = [seat is not None, q is not None, a is not None]
    if any(supplied) and not all(supplied):
        raise ConfigError("debrief: --seat, --q and --a must be supplied together")
    if not any(supplied):
        if kind is not None:
            raise ConfigError("debrief: --kind is valid only when recording an answer")
        print("Ask these at the end, in your own words:\n")
        for item in uxr.debrief_questions():
            print(f"  [{item['signal']}] {item['question']}")
        print("\nRecord an answer with:")
        print('  tablekit debrief --seat S --q "<what you asked>" '
              '--a "<what they said>" [--kind pacing]')
        return 0
    _known_seat(cfg, seat)
    try:
        uxr.record_debrief(led, _seat_id(cfg, seat), q, a, signal=kind)
    except uxr.SignalError as e:
        print(f"debrief: {e}", file=sys.stderr)
        return 2
    print(f"debrief recorded for {_seat_id(cfg, seat)}")
    return 0


def cmd_beat(args):
    cue = _flag(args, "--cue")
    chunks = _integer(_flag(args, "--chunks", 1), "--chunks", 1, 100)
    kind = _flag(args, "--kind")
    text_flag = _flag(args, "--text")
    if kind is not None and kind not in ("scene", "combat", "ooc"):
        raise ConfigError("--kind: expected scene, combat or ooc")
    cfg, led = _ctx(args)
    text = _text_argument(args, "beat", text_flag, required=True)
    seat = _known_seat(cfg, cue, "--cue")
    rec = led.append("ux.beat", words=len(text.split()), chunks=chunks,
                     kind=kind, cued_seat=(seat.id if seat else cue),
                     text=text[:400])
    led.append("event", text=text[:400], actor="GM")
    out = [f"beat {led.current_beat()} recorded ({rec['words']} words)"]
    code = 0
    if cue:
        pid = pairs.new_id("cue", rec["ts"])
        pairs.open_pair(led, "cue", pid, seat=(seat.id if seat else cue),
                        detail=text[:120])
        out.append(f"cue pair {pid} open for {seat.display if seat else cue}")
        problem = cfg.mention_check(text, seat) if cfg else None
        if problem:
            out.append(f"DEFECT undeliverable_cue: {problem}")
            led.append("qc.finding", check="undeliverable_cue", detail=problem,
                       severity="defect", seat=seat.id if seat else cue)
            code = 1
    print("\n".join(out))
    return code


def cmd_inbound(args):
    seat = _flag(args, "--seat")
    text_flag = _flag(args, "--text")
    correlation_id = _flag(args, "--pair")
    if not seat:
        print("inbound: --seat is required", file=sys.stderr)
        return 2
    cfg, led = _ctx(args)
    text = _text_argument(args, "inbound", text_flag, required=True)
    _known_seat(cfg, seat)
    sid = _seat_id(cfg, seat)
    led.append("qa.inbound", seat=sid, chars=len(text), words=len(text.split()))
    try:
        close = pairs.close_one(
            led, ("cue", "checkin"), sid,
            {"cue": "taken", "checkin": "returned"},
            correlation_id=correlation_id, detail="manual non-empty inbound")
    except pairs.PairError as error:
        led.append("qa.route", source="manual", status="quarantined",
                   reason=error.code, seat=sid, detail=str(error),
                   **error.details)
        raise
    led.append("qa.route", source="manual",
               status="routed" if close else "observed",
               reason="obligation_resolved" if close
               else "no_compatible_obligation",
               seat=sid, pair_id=close["id"] if close else None)
    bits = [f"inbound from {sid}"]
    if close:
        bits.append("closed: " + close["id"])
    print(" | ".join(bits))
    return 0


def cmd_roll(args):
    seat = _flag(args, "--seat")
    dc = _flag(args, "--dc")
    if not seat:
        raise ConfigError("roll: --seat is required")
    parsed_dc = _integer(dc, "--dc", 0, 100) if dc is not None else None
    cfg, led = _ctx(args)
    detail = _text_argument(args, "roll", required=False, default="roll called")
    s = _known_seat(cfg, seat)
    pid = pairs.new_id("roll")
    pairs.open_pair(led, "roll", pid, seat=_seat_id(cfg, seat), detail=detail,
                    dc=parsed_dc,
                    rolled_by=(s.rolls if s else None))
    out = [f"roll pair {pid} open — close it with: tablekit consumed {pid}"]
    if dc:
        out.append(f"  DC {dc} recorded (recording is not stating — say the "
                   "number only when it is a reachable long shot)")
    else:
        out.append("  no DC recorded — pass --dc so success/fail stays "
                   "labelable later")
    # The preference surfaces here, at the only moment it can still be acted
    # on. A per-seat preference the GM has to remember gets forgotten, and
    # forgetting it is invisible: the player is put on the spot and says
    # nothing about it.
    if s and not s.rolls_own:
        out.append(f"  {s.display} does not roll their own dice — roll it "
                   "yourself, in the open, and still give them the reveal")
    print("\n".join(out))
    return 0


def cmd_consumed(args):
    outcome = _flag(args, "--outcome", "consumed")
    _cfg, led = _ctx(args)
    pid = _text_argument(args, "consumed", required=True)
    match = [p for p in pairs.open_now(led) if p["id"] == pid]
    if not match:
        print(f"no open pair {pid}", file=sys.stderr)
        return 2
    p = match[0]
    pairs.close_pair(led, p["pair"], pid, outcome, opened_ts=p["opened_ts"])
    print(f"{pid} closed: {outcome}")
    return 0


def cmd_checkin(args):
    seat = _flag(args, "--seat")
    if not seat:
        print("checkin: --seat is required", file=sys.stderr)
        return 2
    cfg, led = _ctx(args)
    _no_positionals(args, "checkin")
    _known_seat(cfg, seat)
    sid = _seat_id(cfg, seat)
    pid = pairs.new_id("checkin")
    pairs.open_pair(led, "checkin", pid, seat=sid, detail="checked on a quiet seat")
    print(f"checked on {sid} — pair {pid} open; resolve it with an inbound "
          f"carrying --pair {pid}")
    return 0


def cmd_turn(args):
    seat = _flag(args, "--seat")
    wait = _finite_number(_flag(args, "--wait", 0), "--wait", 0, 86400)
    if not seat:
        print("turn: --seat is required", file=sys.stderr)
        return 2
    cfg, led = _ctx(args)
    _no_positionals(args, "turn")
    _known_seat(cfg, seat)
    led.append("ux.turn", seat=_seat_id(cfg, seat), wait_s=wait)
    print(f"turn recorded for {_seat_id(cfg, seat)}")
    return 0


def cmd_park(args):
    """Park something for below-the-table investigation.

    Deliberately the cheapest command here: one line, no interruption, no
    judgment. Anything noticed mid-session that is not worth stopping for —
    a number that looked off, a rule that felt wrong, a derivation to check
    against the sheet — belongs in a list rather than in the GM's head, where
    it will not survive the evening.
    """
    topic = _flag(args, "--topic", "note")
    show = _flag(args, "--list", False, takes_value=False)
    done = _flag(args, "--done")
    cfg, led = _ctx(args)
    if show and done is not None:
        raise ConfigError("park: --list and --done are mutually exclusive")
    if show and args:
        raise ConfigError("park: --list does not accept a detail")
    if done is not None and args:
        raise ConfigError("park: provide the completed detail only with --done")
    lot = Ledger(os.path.join(cfg.data_dir if cfg else ".", "parked.jsonl"))

    if show:
        items = lot.read(etype="qa.delta")
        closed = {r.get("detail") for r in lot.read(etype="qa.command")
                  if r.get("cmd") == "park_done"}
        open_items = [i for i in items if i.get("detail") not in closed]
        if not open_items:
            print("parking lot empty")
            return 0
        for i in open_items:
            when = time.strftime("%m-%d", time.localtime(i["ts"]))
            print(f"  [{when}] {i.get('topic')}: {i.get('detail')}")
        return 0

    if done:
        lot.append("qa.command", cmd="park_done", ok=True, detail=done)
        print(f"closed: {done}")
        return 0

    detail = " ".join(args).strip()
    if not detail:
        print('park: needs something to park, e.g. park --topic modifier '
              '"DDB says Initiative +6, we derive +5"\n'
              "       park --list   see what is open\n"
              '       park --done "<the detail>"   close one', file=sys.stderr)
        return 2
    # Both places on purpose: the session file so tonight's report shows it in
    # context, and a persistent lot so it is still there next week. An issues
    # list that resets every session is not an issues list.
    led.append("qa.delta", topic=topic, detail=detail)
    lot.append("qa.delta", topic=topic, detail=detail)
    print(f"parked [{topic}]: {detail}")
    return 0


def cmd_qc(args):
    state_path = _flag(args, "--state")
    do_record = _flag(args, "--record", False, takes_value=False)
    as_json = _flag(args, "--json", False, takes_value=False)
    cfg, led = _ctx(args)
    _no_positionals(args, "qc")
    state = None
    if state_path:
        try:
            with open(state_path) as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise ConfigError(f"--state: cannot read valid JSON from {state_path}: {e}") from e
        if not isinstance(state, dict):
            raise ConfigError("--state: expected a JSON object")
    findings = detector.check(led, cfg, state=state)
    if do_record:
        detector.record(led, findings)
    if as_json:
        print(json.dumps(findings, indent=1))
    else:
        print(detector.format_findings(findings))
    return 1 if findings else 0


def cmd_pairs(args):
    as_json = _flag(args, "--json", False, takes_value=False)
    _cfg, led = _ctx(args)
    _no_positionals(args, "pairs")
    open_p = pairs.open_now(led)
    if as_json:
        print(json.dumps(open_p, indent=1))
        return 0
    if not open_p:
        print("no open pairs")
        return 0
    now = time.time()
    for p in open_p:
        age = int(now - (p["opened_ts"] or now))
        print(f"  {p['id']:<24} {p['pair']:<9} {p.get('seat') or '-':<12} "
              f"open {age // 60}m — {(p.get('detail') or '')[:60]}")
    return 0


def cmd_sweep(args):
    cfg, led = _ctx(args)
    _no_positionals(args, "sweep")
    written = pairs.sweep(led, ttls=(cfg.thresholds if cfg else {}))
    if not written:
        print("nothing to expire")
        return 0
    for w in written:
        print(f"  {w['id']} -> {w['outcome']}")
    return 1


def cmd_report(args):
    as_json = _flag(args, "--json", False, takes_value=False)
    out_path = _flag(args, "--out")
    cfg, led = _ctx(args)
    _no_positionals(args, "report")
    rep = report.build(led, cfg)
    text = report.to_json(rep) if as_json else report.render(rep)
    if out_path:
        from .events import write_atomic
        write_atomic(out_path, text + "\n")
        print(f"wrote {out_path}")
    else:
        print(text)
    return report.exit_code(rep)


def cmd_schema(args):
    _cfg, _led = _ctx(args)
    _no_positionals(args, "schema")
    print(json.dumps({
        "version": __version__,
        "event_types": {k: list(v) for k, v in SCHEMA.items()},
        "pair_kinds": PAIR_KINDS,
        "pair_outcomes": {k: list(v) for k, v in PAIR_OUTCOMES.items()},
        "route_statuses": list(ROUTE_STATUSES),
        "signals": {k: v["means"] for k, v in uxr.SIGNALS.items()},
        "signal_sources": list(uxr.SOURCES),
        "player_command_syntax": None,
        "exit_codes": {"0": "clean", "1": "findings", "2": "refused"},
    }, indent=1))
    return 0


COMMANDS = {
    "init": cmd_init, "beat": cmd_beat,
    "inbound": cmd_inbound, "roll": cmd_roll, "consumed": cmd_consumed,
    "checkin": cmd_checkin, "turn": cmd_turn, "signal": cmd_signal,
    "debrief": cmd_debrief, "qc": cmd_qc, "pairs": cmd_pairs,
    "sweep": cmd_sweep, "park": cmd_park, "report": cmd_report,
    "schema": cmd_schema,
}


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if args[0] in ("-V", "--version"):
        print(__version__)
        return 0
    cmd = args.pop(0)
    fn = COMMANDS.get(cmd)
    if not fn:
        print(f"unknown command {cmd!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    try:
        return fn(args)
    except (ConfigError, SchemaError) as e:
        print(f"{cmd}: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"{cmd}: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"{cmd}: local I/O failed: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
