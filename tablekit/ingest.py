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
  * closes the outcome pairs that this message resolves (a cue answered, a
    checked-on seat returning)

It deliberately does **not** parse player text for commands, tokens or
keywords. Whatever a player types is dialogue, not syntax.

Player prose is not stored by default. The kit needs to know that a seat spoke
and roughly how much; it does not need a transcript, and quietly accumulating
one changes what this tool is. `--keep-text` opts in for a table that wants
the record.
"""

import json
import re
import sys

from . import pairs
from .config import ConfigError, load as load_config
from .events import Ledger


def already_ingested(ledger, msg_id):
    """Has this platform message already been recorded?"""
    if not msg_id:
        return False
    return any(r.get("msg_id") == msg_id
               for r in ledger.read(etype="qa.inbound"))


#: Discord renders roll totals as keycap emoji in the embed's field name
#: (":two::zero:" is 20). Decoding it is the only way to get the number without
#: re-deriving it from the breakdown.
_DIGITS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
           "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def _emoji_number(text):
    parts = re.findall(r":([a-z]+):", text or "")
    if parts and all(p in _DIGITS for p in parts):
        return int("".join(_DIGITS[p] for p in parts))
    return None


def _embed_blob(msg):
    out = []
    for e in (msg.get("embeds") or []):
        for part in (e.get("title"), e.get("description"), e.get("url"),
                     (e.get("author") or {}).get("name"),
                     (e.get("author") or {}).get("url")):
            if part:
                out.append(str(part))
        for f in (e.get("fields") or []):
            out.append(f"{f.get('name', '')} {f.get('value', '')}")
    return " ".join(out)


def parse_relay_roll(msg):
    """Pull the total and the breakdown out of a relayed roll embed.

    Returns `{"label", "total", "breakdown"}` with whatever could be read.
    Spoiler bars are stripped — a table that wraps rolls in `||` still wants
    the number recorded.
    """
    out = {"label": None, "total": None, "breakdown": None}
    for e in (msg.get("embeds") or []):
        if e.get("title") and not out["label"]:
            out["label"] = str(e["title"])
        for f in (e.get("fields") or []):
            if out["total"] is None:
                out["total"] = _emoji_number(f.get("name", ""))
            val = (f.get("value") or "").replace("||", "").strip()
            if val and not out["breakdown"]:
                out["breakdown"] = re.sub(r":[a-z_]+:", "", val).strip()
    return out


def attribute_relay(cfg, msg):
    """Whose roll is this, when a relay bot posted it?

    Roll relays (Beyond20 and friends) post as themselves, in embeds, on
    behalf of a player. Filed naively the whole table's dice land under one
    synthetic seat, nobody's roll pair ever closes, and every human looks
    silent while actually rolling all evening.

    Attribution tries the exact key first: a relayed roll usually links the
    character sheet it came from, and a sheet id cannot be ambiguous the way
    a name can. `seat.sheet_id` in config is what makes that work. Falling
    back to matching the seat's name inside the embed handles tables that have
    not set one — real character names carry decoration ("William Wildmirth
    P3/W4 Hex/Chain"), so the match is a substring, not an equality.

    Returns `(seat_id, text)`, or `(None, blob)` when it cannot tell — and
    when it cannot tell it says so rather than guessing, because a roll
    credited to the wrong seat is worse than one credited to none.
    """
    if not cfg:
        return None, None
    relays = [r.lower().replace(" ", "")
              for r in (cfg.transport.get("roll_relay_bots") or [])]
    author = (msg.get("author") or "").lower().replace(" ", "")
    if not relays or author not in relays:
        return None, None
    blob = _embed_blob(msg)
    if not blob.strip():
        return None, None
    low = blob.lower()
    for seat in cfg.player_seats:
        if seat.sheet_id and str(seat.sheet_id) in blob:
            return seat.id, blob.strip()
    for seat in cfg.player_seats:
        names = {seat.display.lower(), seat.id.lower()} | set(seat.aliases)
        if any(n and n in low for n in names):
            return seat.id, blob.strip()
    return None, blob.strip()


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
    relay_seat, relay_text = attribute_relay(cfg, msg)
    via = None
    if relay_text is not None:
        # A relayed roll. Keep its text regardless of `keep_text`: it is dice
        # arithmetic, not the player's prose, and it is the evidence that
        # closes the roll pair.
        text = relay_text
        via = author
        if relay_seat is None:
            ledger.append("qa.command", cmd="relay_unattributed", ok=False,
                          detail=f"{author} posted a roll no seat name matched",
                          msg_id=msg_id)
            return []
        sid = relay_seat
        seat = cfg.seat(sid)
        roll = parse_relay_roll(msg)
    else:
        seat = cfg.seat(author) if cfg else None
        sid = seat.id if seat else author
    written = [ledger.append("qa.inbound", ts=ts, seat=sid, chars=len(text),
                             words=len(text.split()), msg_id=msg_id, via=via,
                             text=(text[:400] if (keep_text or via) else None))]
    if via:
        r = roll
        written.append(ledger.append(
            "act", ts=ts, actor=(seat.display if seat else sid),
            text=f"{r['label'] or 'roll'}: {r['total'] if r['total'] is not None else '?'}"
                 + (f" ({r['breakdown']})" if r["breakdown"] else ""),
            roll_total=r["total"], roll_label=r["label"], via=via))
    # Note what is NOT here: no scan of the player's words for tokens or
    # keywords. Inbound text is data about the table, never a command
    # surface. Signals are recorded deliberately by a classifier or by the
    # debrief — see tablekit.uxr.
    closes = [("cue", "taken"), ("checkin", "returned")]
    if via:
        # A relayed roll is the roll arriving — that is what a roll pair is
        # waiting for.
        closes.append(("roll", "consumed"))
    for kind, outcome in closes:
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
            ingest_message(cfg, ledger, msg, keep_text)
    except KeyboardInterrupt:
        pass
    finally:
        ledger.append("qa.listener", state="down", detail="ingest detached")
    return 0


if __name__ == "__main__":
    sys.exit(main())
