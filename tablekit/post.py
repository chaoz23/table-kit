"""Outbound — say something to the table, and record that you did.

Two jobs beyond "send the message".

**1. Mandatory mentions.** If a beat cues an agent seat, that seat's literal
mention is guaranteed to be in the outgoing text — prepended if the author did
not include it. This is not a lint that complains afterwards; it is a repair
that happens before the message leaves, because the failure it prevents is
invisible at run time. The repair is recorded in the session file, so "how
often does the GM forget the mention" stays an answerable question.

**2. Splitting is a last resort, and it is reported.** Chat platforms cap
message length. A beat that arrives as three messages is read as three beats,
and the table answers the first one — which is a craft defect introduced
purely by transport. So the splitter breaks on paragraph boundaries, and every
split is recorded as a `qa.post` with its chunk count so the report can show
how often the plumbing reshaped the play.

Discord is the only transport implemented here. `send_fn` is the seam: pass
your own callable and the rest of the kit does not care what is underneath.
"""

import json
import time
import urllib.error
import urllib.request

#: Discord's hard cap is 2000; the margin absorbs the mention prefix and any
#: platform-side formatting without a surprise 400 mid-session.
CHUNK_LIMIT = 1900


def split(text, limit=CHUNK_LIMIT):
    """Split on paragraph boundaries, then on lines, then hard.

    Never mid-word: a beat that breaks mid-sentence across two messages reads
    as a transport glitch, which pulls the table out of the fiction more than
    the extra message does.
    """
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if len(para) > limit:
            for line in para.split("\n"):
                while len(line) > limit:
                    cut = line.rfind(" ", 0, limit) or limit
                    if cur:
                        chunks.append(cur)
                        cur = ""
                    chunks.append(line[:cut])
                    line = line[cut:].lstrip()
                if len(cur) + len(line) + 1 > limit:
                    chunks.append(cur)
                    cur = line
                else:
                    cur = f"{cur}\n{line}" if cur else line
        elif len(cur) + len(para) + 2 > limit:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def ensure_mention(cfg, text, seat):
    """Return `(text, repaired)` — the text guaranteed to reach `seat`."""
    if not seat or seat.kind != "agent" or not seat.mention:
        return text, False
    if seat.mention in text:
        return text, False
    return f"{seat.mention} {text}", True


def discord_send(cfg, text):
    """POST one message. Returns the platform message id."""
    url = (f"https://discord.com/api/v10/channels/"
           f"{cfg.transport['channel_id']}/messages")
    body = json.dumps({"content": text}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bot {cfg.token()}",
                 "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (table-kit, 0.1)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r).get("id")


def post(cfg, ledger, text, cue=None, kind=None, send_fn=None):
    """Say `text` to the table.

    `cue` names the seat this beat is addressed to. Returns a result dict; the
    caller decides what to do with a failure, but the failure is in the
    session file either way.
    """
    send = send_fn or discord_send
    seat = cfg.seat(cue) if cue else None
    text, repaired = ensure_mention(cfg, text, seat)
    chunks = split(text)

    t0 = time.time()
    ids, error = [], None
    for c in chunks:
        try:
            ids.append(send(cfg, c))
        except (urllib.error.URLError, OSError, ValueError) as e:
            error = str(e)
            break
    latency_ms = int((time.time() - t0) * 1000)

    if error:
        ledger.append("qa.post_failed", error=error, chunks_sent=len(ids),
                      chunks_planned=len(chunks))
        return {"ok": False, "error": error, "chunks": len(chunks)}

    ledger.append("qa.post", ok=True, chars=len(text), chunks=len(chunks),
                  latency_ms=latency_ms, mention_repaired=repaired or None,
                  message_ids=ids or None)
    ledger.append("ux.beat", words=len(text.split()), chunks=len(chunks),
                  kind=kind, cued_seat=(seat.id if seat else cue),
                  text=text[:400])
    ledger.append("event", text=text[:400], actor="GM")
    if repaired:
        # Not a finding — nothing broke. But an author who keeps needing the
        # repair is one `post()` bypass away from a stranded seat.
        ledger.append("qa.command", cmd="mention_repair", ok=True,
                      seat=seat.id)
    if seat:
        from . import pairs
        pairs.open_pair(ledger, "cue", f"cue-{int(t0)}", seat=seat.id,
                        detail=text[:120])
    return {"ok": True, "chunks": len(chunks), "mention_repaired": repaired,
            "latency_ms": latency_ms, "message_ids": ids}
