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
import os
import sys
import time

from . import detector, pairs, report, ux, uxr
from .config import ConfigError, load as load_config
from .events import SCHEMA, Ledger, PAIR_KINDS, SchemaError

__version__ = "0.1.1"

USAGE = """tablekit — instrumentation for a live hybrid table

  tablekit init [path]              write a starter table.json
  tablekit markers                  the player marker card (paste at the table)

  during play
    tablekit beat "<text>" [--cue SEAT] [--chunks N] [--kind scene|combat|ooc]
    tablekit inbound --seat S --text "<what they said>"
    tablekit roll --seat S "<what for>"        called for a roll
    tablekit consumed <pair-id> [--outcome consumed]
    tablekit checkin --seat S                  checked on a quiet seat
    tablekit turn --seat S [--wait N]          a seat got the floor
    tablekit qc [--state FILE] [--record]      run the checks
    tablekit pairs                             what is still open
    tablekit sweep                             expire what has timed out

  after play
    tablekit report [--json] [--out FILE]
    tablekit schema                   the event schema, as JSON

  options
    --config PATH   table config (default $TABLE_CONFIG or ./table.json)
    --session NAME  session name -> <data_dir>/<name>.jsonl
    --ledger PATH   use this file directly, ignore config paths
    --version
"""


def _flag(args, name, default=None, takes_value=True):
    if name not in args:
        return default
    i = args.index(name)
    args.pop(i)
    if not takes_value:
        return True
    return args.pop(i) if i < len(args) else default


def _ctx(args):
    """Resolve (config, ledger). Config is optional; a ledger is not."""
    cfg_path = _flag(args, "--config")
    session = _flag(args, "--session")
    ledger_path = _flag(args, "--ledger")
    cfg = None
    try:
        cfg = load_config(cfg_path)
    except ConfigError:
        if cfg_path:
            raise
    if not ledger_path:
        if cfg:
            ledger_path = cfg.ledger_path(session)
        else:
            ledger_path = os.environ.get("TABLE_LEDGER", "./table-data/session.jsonl")
    return cfg, Ledger(ledger_path)


def _seat_id(cfg, name):
    if not cfg or not name:
        return name
    s = cfg.seat(name)
    return s.id if s else name


def cmd_init(args):
    path = args[0] if args else "table.json"
    if os.path.exists(path):
        print(f"{path} already exists — not overwriting", file=sys.stderr)
        return 2
    example = os.path.join(os.path.dirname(__file__), "table.example.json")
    with open(example) as f:
        text = f.read()
    with open(path, "w") as f:
        f.write(text)
    print(f"wrote {path}")
    print("Edit it: seats, and a literal 'mention' for every agent seat.")
    return 0


def cmd_markers(_args):
    print(uxr.help_text())
    return 0


def cmd_beat(args):
    cue = _flag(args, "--cue")
    chunks = int(_flag(args, "--chunks", 1))
    kind = _flag(args, "--kind")
    text = args[0] if args else ""
    if not text:
        print("beat: needs the text of the beat", file=sys.stderr)
        return 2
    cfg, led = _ctx(args)
    seat = cfg.seat(cue) if (cfg and cue) else None
    rec = led.append("ux.beat", words=len(text.split()), chunks=chunks,
                     kind=kind, cued_seat=(seat.id if seat else cue),
                     text=text[:400])
    led.append("event", text=text[:400], actor="GM")
    out = [f"beat {led.current_beat()} recorded ({rec['words']} words)"]
    code = 0
    if cue:
        pid = f"cue-{int(rec['ts'])}"
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
    text = _flag(args, "--text") or (args[0] if args else "")
    if not seat:
        print("inbound: --seat is required", file=sys.stderr)
        return 2
    cfg, led = _ctx(args)
    sid = _seat_id(cfg, seat)
    led.append("qa.inbound", seat=sid, chars=len(text), words=len(text.split()))
    marks = uxr.record(led, sid, text)
    closed = []
    for p in pairs.open_now(led, "cue"):
        if p.get("seat") == sid:
            pairs.close_pair(led, "cue", p["id"], "taken", opened_ts=p["opened_ts"])
            closed.append(p["id"])
    for p in pairs.open_now(led, "checkin"):
        if p.get("seat") == sid:
            pairs.close_pair(led, "checkin", p["id"], "returned",
                             opened_ts=p["opened_ts"])
            closed.append(p["id"])
    bits = [f"inbound from {sid}"]
    if marks:
        bits.append("markers: " + ", ".join("!" + m["marker"] for m in marks))
    if closed:
        bits.append("closed: " + ", ".join(closed))
    print(" | ".join(bits))
    return 0


def cmd_roll(args):
    seat = _flag(args, "--seat")
    detail = args[0] if args else "roll called"
    cfg, led = _ctx(args)
    pid = f"roll-{int(time.time())}"
    pairs.open_pair(led, "roll", pid, seat=_seat_id(cfg, seat), detail=detail)
    print(f"roll pair {pid} open — close it with: tablekit consumed {pid}")
    return 0


def cmd_consumed(args):
    outcome = _flag(args, "--outcome", "consumed")
    if not args:
        print("consumed: needs a pair id (see `tablekit pairs`)", file=sys.stderr)
        return 2
    pid = args[0]
    _cfg, led = _ctx(args)
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
    sid = _seat_id(cfg, seat)
    pid = f"checkin-{int(time.time())}"
    pairs.open_pair(led, "checkin", pid, seat=sid, detail="checked on a quiet seat")
    print(f"checked on {sid} — pair {pid} closes when they next speak")
    return 0


def cmd_turn(args):
    seat = _flag(args, "--seat")
    wait = float(_flag(args, "--wait", 0) or 0)
    if not seat:
        print("turn: --seat is required", file=sys.stderr)
        return 2
    cfg, led = _ctx(args)
    led.append("ux.turn", seat=_seat_id(cfg, seat), wait_s=wait)
    print(f"turn recorded for {_seat_id(cfg, seat)}")
    return 0


def cmd_qc(args):
    state_path = _flag(args, "--state")
    do_record = _flag(args, "--record", False, takes_value=False)
    as_json = _flag(args, "--json", False, takes_value=False)
    cfg, led = _ctx(args)
    state = None
    if state_path:
        with open(state_path) as f:
            state = json.load(f)
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
    rep = report.build(led, cfg)
    text = report.to_json(rep) if as_json else report.render(rep)
    if out_path:
        from .events import write_atomic
        write_atomic(out_path, text + "\n")
        print(f"wrote {out_path}")
    else:
        print(text)
    return report.exit_code(rep)


def cmd_schema(_args):
    print(json.dumps({
        "version": __version__,
        "event_types": {k: list(v) for k, v in SCHEMA.items()},
        "pair_kinds": PAIR_KINDS,
        "markers": {k: v["meaning"] for k, v in uxr.MARKERS.items()},
        "exit_codes": {"0": "clean", "1": "findings", "2": "refused"},
    }, indent=1))
    return 0


COMMANDS = {
    "init": cmd_init, "markers": cmd_markers, "beat": cmd_beat,
    "inbound": cmd_inbound, "roll": cmd_roll, "consumed": cmd_consumed,
    "checkin": cmd_checkin, "turn": cmd_turn, "qc": cmd_qc, "pairs": cmd_pairs,
    "sweep": cmd_sweep, "report": cmd_report, "schema": cmd_schema,
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


if __name__ == "__main__":
    sys.exit(main())
