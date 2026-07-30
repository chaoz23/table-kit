"""Inbound — turn what the table said into events.

The listener that watches the chat platform is a few dozen lines of
JavaScript (see `transport/discord/listen.mjs`) and it deliberately knows
nothing about this schema. It prints one JSON object per message to stdout;
this module reads them and does all the interpretation:

    node transport/discord/listen.mjs | python3 -m tablekit.ingest

Keeping schema knowledge on one side of that pipe means the listener can be
replaced — a different platform, a webhook, a file tail — without any risk of
two implementations of "what counts as a marker" drifting apart.

What ingestion does per message:

  * records `qa.inbound` — who spoke and how much, never the prose itself
  * extracts any UXR markers and anchors them to the current beat
  * closes the outcome pairs that this message resolves (a cue answered, a
    checked-on seat returning)

Player prose is not stored by default. The kit needs to know that a seat spoke
and roughly how much; it does not need a transcript, and quietly accumulating
one changes what this tool is. `--keep-text` opts in for a table that wants
the record.
"""

import json
import sys

from . import pairs, uxr
from .config import ConfigError, load as load_config
from .events import Ledger


def already_ingested(ledger, msg_id):
    """Has this platform message already been recorded?"""
    if not msg_id:
        return False
    return any(r.get("msg_id") == msg_id
               for r in ledger.read(etype="qa.inbound"))


def ingest_message(cfg, ledger, msg, keep_text=False):
    """One inbound message. Returns the events written.

    **Idempotent when the message carries an `id`.** Push listeners deliver
    once, but polling transports re-read the same window constantly — a GM
    checking the channel twice between beats is the normal case, not an edge
    case. Without this, every re-read would double a seat's line count and
    quietly corrupt the participation numbers the report is built on.
    """
    author = msg.get("author") or msg.get("username") or ""
    text = msg.get("content") or msg.get("text") or ""
    ts = msg.get("ts")
    msg_id = msg.get("id")
    if cfg and cfg.is_gm(author):
        # The GM's own posts arrive back through the listener. They are
        # already recorded by `post()`; recording them again would double
        # every beat and halve every apparent player share.
        return []
    if already_ingested(ledger, msg_id):
        return []
    seat = cfg.seat(author) if cfg else None
    sid = seat.id if seat else author
    written = [ledger.append("qa.inbound", ts=ts, seat=sid, chars=len(text),
                             words=len(text.split()), msg_id=msg_id,
                             text=(text[:400] if keep_text else None))]
    written += uxr.record(ledger, sid, text, ts=ts)
    for kind, outcome in (("cue", "taken"), ("checkin", "returned")):
        for p in pairs.open_now(ledger, kind):
            if p.get("seat") == sid:
                written.append(pairs.close_pair(
                    ledger, kind, p["id"], outcome, ts=ts,
                    opened_ts=p["opened_ts"]))
    return written


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    keep_text = "--keep-text" in args
    if keep_text:
        args.remove("--keep-text")
    cfg_path = args[0] if args else None
    try:
        cfg = load_config(cfg_path)
    except ConfigError as e:
        print(f"ingest: {e}", file=sys.stderr)
        return 2
    ledger = Ledger(cfg.ledger_path())
    ledger.append("qa.listener", state="up", detail="ingest attached")
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # A non-JSON line is the listener talking to a human (a
                # connection notice). Pass it through rather than swallowing
                # it — a silent ingest is indistinguishable from a dead one.
                print(line, file=sys.stderr)
                continue
            evs = ingest_message(cfg, ledger, msg, keep_text)
            marks = [e for e in evs if e["type"] == "uxr.marker"]
            if marks:
                print(f"{msg.get('author')}: "
                      + ", ".join("!" + m["marker"] for m in marks))
    except KeyboardInterrupt:
        pass
    finally:
        ledger.append("qa.listener", state="down", detail="ingest detached")
    return 0


if __name__ == "__main__":
    sys.exit(main())
