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
    """Pull the total, the breakdown, and the SHEET'S OWN MODIFIER out of a
    relayed roll embed.

    The modifier is the interesting one. A relayed roll carries the number the
    character sheet computed — "Initiative (+6)" — for a named check, on a
    named sheet. Anything that derives modifiers from the same sheet can be
    checked against it, for free, on every roll of every session, without
    anyone doing extra work.

    That is a real correctness oracle and it is worth capturing even if
    nothing consumes it yet: the comparison can be run after the session, but
    only if the observation was recorded during it.

    Returns `{"label", "check", "modifier", "total", "breakdown"}`.
    """
    out = {"label": None, "check": None, "modifier": None,
           "total": None, "breakdown": None}
    for e in (msg.get("embeds") or []):
        if e.get("title") and not out["label"]:
            out["label"] = str(e["title"])
            # "Initiative (+6)" / "Perception (-1)" / "Athletics (+3)"
            m = re.match(r"\s*(.+?)\s*\(\s*([+-]\s*\d+)\s*\)\s*$", out["label"])
            if m:
                out["check"] = m.group(1).strip()
                out["modifier"] = int(m.group(2).replace(" ", ""))
            else:
                out["check"] = out["label"].strip()
        for f in (e.get("fields") or []):
            if out["total"] is None:
                out["total"] = _emoji_number(f.get("name", ""))
            val = (f.get("value") or "").replace("||", "").strip()
            if val and not out["breakdown"]:
                out["breakdown"] = re.sub(r":[a-z_]+:", "", val).strip()
    return out


#: Explicit "this is my total" phrasings. When one of these fires, a number in
#: an otherwise chatty message is still trustworthy.
_TOTAL_CUE = re.compile(
    r"\b(total|totals?\s+to|=|for\s+a|rolled|roll(?:s|ed)?\s+a|got|i\s+get|"
    r"that'?s\s+a?|nat(?:ural)?)\b", re.I)
_NAT = re.compile(r"\bnat(?:ural)?\s*(\d{1,2})\b", re.I)
_NUM = re.compile(r"(?<![\w.])(-?\d{1,3})(?![\w.])")


def detect_typed_roll(text, max_words=14):
    """Did a player just say their roll in plain language?

    Not every table has a relay, and the same table will not have one every
    night — somebody joins from a phone, an extension is not installed, a
    browser is signed out. So a roll arriving as ordinary text is the normal
    case to support, not the fallback.

    Returns `{"total", "confidence"}` where confidence is:

      * ``high``   — safe to consume automatically
      * ``low``    — a number is present but the reading is ambiguous, so the
                     GM is asked to confirm rather than the kit guessing
      * ``none``   — nothing that looks like a roll

    The gate is deliberately conservative. A wrong total silently consumed is
    far worse than one the GM had to confirm: it corrupts the ledger and
    nobody finds out until the arithmetic stops making sense.
    """
    t = (text or "").strip()
    if not t:
        return {"total": None, "confidence": "none"}
    nat = _NAT.search(t)
    if nat:
        return {"total": int(nat.group(1)), "confidence": "high"}
    nums = [int(n) for n in _NUM.findall(t)]
    if not nums:
        return {"total": None, "confidence": "none"}
    words = len(t.split())
    # "14" or "14!" on its own is unambiguous.
    if len(nums) == 1 and words <= 3:
        return {"total": nums[0], "confidence": "high"}
    cued = bool(_TOTAL_CUE.search(t))
    if len(nums) == 1 and cued and words <= max_words:
        return {"total": nums[0], "confidence": "high"}
    # "18 + 3 = 21" — an explicit equals wins over the earlier numbers.
    eq = re.search(r"=\s*(-?\d{1,3})\b", t)
    if eq:
        return {"total": int(eq.group(1)), "confidence": "high"}
    return {"total": nums[0], "confidence": "low"}


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
            roll_total=r["total"], roll_label=r["label"], roll_check=r["check"],
            sheet_modifier=r["modifier"], sheet_id=(seat.sheet_id if seat else None),
            via=via))
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

    # No relay this time? A roll can still arrive as ordinary text — somebody
    # joined from a phone, the extension is not installed, the browser is
    # signed out. Detect it, but only while a roll is actually outstanding for
    # this seat, and only consume it when the reading is unambiguous.
    if not via:
        open_rolls = [p for p in pairs.open_now(ledger, "roll")
                      if p.get("seat") == sid]
        if open_rolls:
            det = detect_typed_roll(text)
            p = open_rolls[0]
            if det["confidence"] == "high":
                written.append(ledger.append(
                    "act", ts=ts, actor=(seat.display if seat else sid),
                    text=f"{p.get('detail') or 'roll'}: {det['total']}",
                    roll_total=det["total"], via="typed"))
                written.append(pairs.close_pair(
                    ledger, "roll", p["id"], "consumed", ts=ts,
                    opened_ts=p["opened_ts"], detail="typed in chat"))
            elif det["confidence"] == "low":
                # Ask, do not assume. A wrong total silently consumed corrupts
                # the ledger and nobody notices until the arithmetic stops
                # making sense.
                written.append(ledger.append(
                    "qc.finding", ts=ts, check="roll_needs_confirming",
                    severity="attention", seat=sid,
                    detail=f"{seat.display if seat else sid} said something with "
                           f"a number in it while a roll was open — was that "
                           f"a {det['total']}?",
                    evidence=text[:120]))
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
