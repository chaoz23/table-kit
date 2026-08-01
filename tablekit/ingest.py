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
  * resolves at most one compatible outcome pair, and only when correlation
    is explicit or unique

It deliberately does **not** parse player text for commands, tokens or
keywords. Whatever a player types is dialogue, not syntax.

Player prose is not stored by default. The kit needs to know that a seat spoke
and roughly how much; it does not need a transcript, and quietly accumulating
one changes what this tool is. `--keep-text` opts in for a table that wants
the record.
"""

import hashlib
import json
import math
import re
import sys

from . import pairs
from .config import ConfigError, load as load_config, normalize_identity
from .events import Ledger, SchemaError


def _native_id(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int)):
        value = str(value).strip()
        return value or None
    return None


def _source_fingerprint(msg):
    """Hash source evidence so one native ID cannot silently change meaning."""
    try:
        payload = json.dumps(msg, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=True).encode()
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload).hexdigest()


def already_ingested(ledger, msg_id, source="discord"):
    """Has this source-native message receipt already been committed?"""
    msg_id = _native_id(msg_id)
    if not msg_id:
        return False
    return any(r.get("msg_id") == msg_id
               and r.get("source", source) == source
               for r in ledger.read(etype="qa.inbound"))


def _route_history(ledger, source, source_id):
    if not source_id:
        return []
    return [r for r in ledger.read(etype="qa.route")
            if r.get("source") == source and r.get("source_id") == source_id]


def _append_route(ledger, source, source_id, status, reason, **fields):
    payload = dict(source=source, source_id=source_id, status=status,
                   reason=reason, **fields)
    if source_id:
        rec, created = ledger.append_once(
            "qa.route",
            {"source": source, "source_id": source_id,
             "status": status, "reason": reason}, **payload)
        return rec if created else None
    return ledger.append("qa.route", **payload)


def _append_effect_once(ledger, etype, source, source_id, **fields):
    if source_id:
        rec, created = ledger.append_once(
            etype, {"source": source, "source_id": source_id},
            source=source, source_id=source_id, **fields)
        return rec if created else None
    return ledger.append(etype, source=source, **fields)


#: Discord renders roll totals as keycap emoji in the embed's field name
#: (":two::zero:" is 20). Decoding it is the only way to get the number without
#: re-deriving it from the breakdown.
_DIGITS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
           "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def _emoji_number(text):
    if not isinstance(text, str):
        return None
    parts = re.findall(r":([a-z]+):", text or "")
    if parts and len(parts) <= 4 and all(p in _DIGITS for p in parts):
        return int("".join(_DIGITS[p] for p in parts))
    return None


def _embed_blob(msg):
    out = []
    embeds = msg.get("embeds")
    embeds = embeds[:32] if isinstance(embeds, list) else []
    for e in embeds:
        if not isinstance(e, dict):
            continue
        author = e.get("author") if isinstance(e.get("author"), dict) else {}
        for part in (e.get("title"), e.get("description"), e.get("url"),
                     author.get("name"), author.get("url")):
            if isinstance(part, (str, int, float)) and not isinstance(part, bool):
                out.append(str(part)[:1000])
        fields = e.get("fields")
        fields = fields[:32] if isinstance(fields, list) else []
        for f in fields:
            if not isinstance(f, dict):
                continue
            name = f.get("name") if isinstance(f.get("name"), str) else ""
            value = f.get("value") if isinstance(f.get("value"), str) else ""
            out.append(f"{name[:1000]} {value[:1000]}")
    return " ".join(out)[:8000]


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

    Returns the source-observed fields plus a narrow validation result.  This
    code does not re-derive system totals.  When the source explicitly exposes
    a single natural die and its declared die size, the natural must fall in
    that range; broader system validation belongs upstream.
    """
    out = {"label": None, "check": None, "modifier": None,
           "total": None, "breakdown": None, "die": None,
           "natural": None, "valid": True, "validation": "source_observed"}
    embeds = msg.get("embeds")
    embeds = embeds[:32] if isinstance(embeds, list) else []
    for e in embeds:
        if not isinstance(e, dict):
            continue
        if isinstance(e.get("title"), str) and e["title"].strip() and not out["label"]:
            out["label"] = e["title"][:400]
            # "Initiative (+6)" / "Perception (-1)" / "Athletics (+3)"
            m = re.match(r"\s*(.+?)\s*\(\s*([+-]\s*\d+)\s*\)\s*$", out["label"])
            if m:
                out["check"] = m.group(1).strip()
                out["modifier"] = int(m.group(2).replace(" ", ""))
            else:
                out["check"] = out["label"].strip()
        fields = e.get("fields")
        fields = fields[:32] if isinstance(fields, list) else []
        for f in fields:
            if not isinstance(f, dict):
                continue
            field_name = f.get("name")
            field_total = _emoji_number(
                field_name[:1000] if isinstance(field_name, str) else "")
            if out["total"] is None and field_total is not None:
                out["total"] = field_total
                raw_value = f.get("value")
                val = (raw_value.replace("||", "").strip()
                       if isinstance(raw_value, str) else "")
                if val:
                    out["breakdown"] = re.sub(
                        r":[a-z_]+:", "", val).strip()[:400]
    blob = " ".join(part for part in (out["label"], out["breakdown"]) if part)
    die = re.search(r"\b1d(\d{1,4})\b", blob, re.I)
    if die:
        sides = int(die.group(1))
        if sides < 2 or sides > 1000:
            out.update(valid=False, validation="invalid_declared_die")
            return out
        out["die"] = {"count": 1, "sides": sides}
        natural = re.search(r"\((\d{1,4})\)", out["breakdown"] or "")
        if not natural:
            natural = re.search(r"\bnat(?:ural)?\s*(\d{1,4})\b", blob, re.I)
        if natural:
            out["natural"] = int(natural.group(1))
            if not 1 <= out["natural"] <= sides:
                out.update(valid=False, validation="natural_out_of_range")
    else:
        # A relay that explicitly labels a value as a natural is describing a
        # d20 even when its pretty-printed breakdown omits ``1d20``. Do not
        # accept physically impossible values merely because the die token is
        # missing.
        natural = re.search(r"\bnat(?:ural)?\s*(\d{1,4})\b", blob, re.I)
        if natural:
            out["die"] = {"count": 1, "sides": 20}
            out["natural"] = int(natural.group(1))
            if not 1 <= out["natural"] <= 20:
                out.update(valid=False, validation="natural_out_of_range")
    return out


#: Explicit "this is my total" phrasings. When one of these fires, a number in
#: an otherwise chatty message is still trustworthy.
_TOTAL_CUE = re.compile(
    r"\b(total|totals?\s+to|=|for\s+a|rolled|roll(?:s|ed)?\s+a|got|i\s+get|"
    r"that'?s\s+a?|nat(?:ural)?)\b", re.I)
_NAT = re.compile(r"\bnat(?:ural)?\s*(\d{1,4})\b", re.I)
_NUM = re.compile(r"(?<![\w.])(-?\d{1,3})(?![\w.])")
_NON_ROLL_CONTEXT = re.compile(
    r"\b(damage|dmg|heal(?:ing|ed|s)?|hit\s*points?|hp|feet|foot|ft\.?|"
    r"squares?|rounds?|minutes?|hours?|gold|gp)\b", re.I)


def detect_typed_roll(text, max_words=14):
    """Did a player just say their roll in plain language?

    Not every table has a relay, and the same table will not have one every
    night — somebody joins from a phone, an extension is not installed, a
    browser is signed out. So a roll arriving as ordinary text is the normal
    case to support, not the fallback.

    Returns ``{"total", "confidence", "reason", "provenance"}``.  Every
    prose result is advisory; even a high-confidence parse needs an explicit
    operator/source correlation before it may resolve an obligation.

    Confidence is:

      * ``high``   — strongly parsed, but still advisory and never consumed
      * ``low``    — a number is present but the reading is ambiguous, so the
                     GM is asked to confirm rather than the kit guessing
      * ``none``   — nothing that looks like a roll

    The gate is deliberately conservative. A wrong total silently consumed is
    far worse than one the GM had to confirm: it corrupts the ledger and
    nobody finds out until the arithmetic stops making sense.
    """
    t = (text or "").strip()
    if not t:
        return {"total": None, "confidence": "none",
                "reason": "blank", "provenance": "prose_advisory"}
    if _NON_ROLL_CONTEXT.search(t):
        return {"total": None, "confidence": "none",
                "reason": "non_roll_context", "provenance": "prose_advisory"}
    nat = _NAT.search(t)
    if nat:
        natural = int(nat.group(1))
        if not 1 <= natural <= 20:
            return {"total": natural, "confidence": "invalid",
                    "reason": "natural_out_of_range",
                    "provenance": "prose_advisory", "die": "d20"}
        return {"total": natural, "confidence": "high",
                "reason": "explicit_natural", "provenance": "prose_advisory",
                "die": "d20"}
    nums = [int(n) for n in _NUM.findall(t)]
    if not nums:
        return {"total": None, "confidence": "none",
                "reason": "no_number", "provenance": "prose_advisory"}
    words = len(t.split())
    # "14" or "14!" on its own is unambiguous.
    if len(nums) == 1 and words <= 3:
        return {"total": nums[0], "confidence": "high",
                "reason": "single_number", "provenance": "prose_advisory"}
    cued = bool(_TOTAL_CUE.search(t))
    if len(nums) == 1 and cued and words <= max_words:
        return {"total": nums[0], "confidence": "high",
                "reason": "explicit_total", "provenance": "prose_advisory"}
    # "18 + 3 = 21" — an explicit equals wins over the earlier numbers.
    eq = re.search(r"=\s*(-?\d{1,3})\b", t)
    if eq:
        return {"total": int(eq.group(1)), "confidence": "high",
                "reason": "explicit_equation", "provenance": "prose_advisory"}
    return {"total": nums[0], "confidence": "low",
            "reason": "ambiguous_number", "provenance": "prose_advisory"}


def attribute_relay(cfg, msg):
    """Whose roll is this, when a relay bot posted it?

    Roll relays (Beyond20 and friends) post as themselves, in embeds, on
    behalf of a player. Filed naively the whole table's dice land under one
    synthetic seat, nobody's roll pair ever closes, and every human looks
    silent while actually rolling all evening.

    Attribution first uses an exact source-native sheet ID extracted from the
    structured character URL.  Its only fallback is an exact normalized
    configured identity from a structured name field.  It never searches for
    seat names as substrings: ``Will`` must not capture ``William``.

    The return object distinguishes a non-relay from an unverified,
    unattributed, ambiguous, or attributed relay.  A configured display name
    is not enough to grant relay authority: ``is_bot`` must be true.  Stable
    host mapping of the relay's source-native principal ID is deferred to
    PORT-002/003, but the ID is preserved on every receipt.
    """
    if not cfg:
        return {"is_relay": False, "seat": None, "text": None,
                "reason": "not_configured"}
    relays = [normalize_identity(r).replace(" ", "")
              for r in (cfg.transport.get("roll_relay_bots") or [])]
    author = normalize_identity(msg.get("author") or "").replace(" ", "")
    if not relays or author not in relays:
        return {"is_relay": False, "seat": None, "text": None,
                "reason": "not_configured"}
    blob = _embed_blob(msg)
    if msg.get("is_bot") is not True:
        return {"is_relay": True, "seat": None, "text": blob.strip() or None,
                "reason": "relay_identity_unverified"}
    if not blob.strip():
        return {"is_relay": True, "seat": None, "text": None,
                "reason": "relay_evidence_missing"}

    sheet_ids = set()
    name_candidates = set()
    explicit_sheet = _native_id(msg.get("sheet_id"))
    if explicit_sheet:
        sheet_ids.add(explicit_sheet)
    embeds = msg.get("embeds")
    embeds = embeds[:32] if isinstance(embeds, list) else []
    for embed in embeds:
        if not isinstance(embed, dict):
            continue
        embed_author = (embed.get("author")
                        if isinstance(embed.get("author"), dict) else {})
        urls = [embed.get("url"), embed_author.get("url")]
        for url in urls:
            if not isinstance(url, str):
                continue
            match = re.search(r"/characters/([A-Za-z0-9_-]+)(?:[/?#]|$)",
                              url[:2000])
            if match:
                sheet_ids.add(match.group(1))
        author_name = embed_author.get("name")
        if isinstance(author_name, str) and author_name.strip():
            name_candidates.add(author_name[:400])
        title = embed.get("title")
        if isinstance(title, str) and title.strip():
            # Beyond20-style "Rowan: Perception" has a structured actor
            # prefix.  The whole prefix must be a configured identity.
            name_candidates.add(title[:400].split(":", 1)[0])

    sheet_matches = [seat for seat in cfg.player_seats
                     if seat.sheet_id is not None
                     and str(seat.sheet_id) in sheet_ids]
    if len(sheet_matches) == 1:
        return {"is_relay": True, "seat": sheet_matches[0],
                "text": blob.strip(), "reason": "sheet_id"}
    if len(sheet_matches) > 1:  # config validation should make this unreachable
        return {"is_relay": True, "seat": None, "text": blob.strip(),
                "reason": "relay_attribution_ambiguous"}

    name_matches = set()
    for candidate in name_candidates:
        seat = cfg.seat(candidate)
        if seat and seat in cfg.player_seats:
            name_matches.add(seat)
    if len(name_matches) == 1:
        return {"is_relay": True, "seat": name_matches.pop(),
                "text": blob.strip(), "reason": "exact_alias"}
    if len(name_matches) > 1:
        return {"is_relay": True, "seat": None, "text": blob.strip(),
                "reason": "relay_attribution_ambiguous"}
    return {"is_relay": True, "seat": None, "text": blob.strip(),
            "reason": "relay_unattributed"}


REPAIRABLE_ROUTE_REASONS = {"relay_unattributed", "unknown_principal"}


def _identity(cfg, msg):
    author = msg.get("author") or msg.get("username") or ""
    author = author if isinstance(author, str) else ""
    is_bot = msg.get("is_bot") if isinstance(msg.get("is_bot"), bool) else None
    seat = cfg.seat(author) if cfg and author else None
    reason = None
    if seat and is_bot is True and seat.kind == "human":
        seat = None
        reason = "role_mismatch"
    elif seat and is_bot is False and seat.kind == "agent":
        seat = None
        reason = "role_mismatch"
    role = "bot" if is_bot is True else ("user" if is_bot is False else "unknown")
    return {
        "author": author,
        "principal_id": _native_id(msg.get("author_id")) or "unknown",
        "is_bot": is_bot,
        "role": role,
        "seat": seat,
        "identity_source": "alias_fallback" if seat else "unknown",
        "reason": reason,
    }


def _existing_resolution(ledger, source, source_id):
    if not source_id:
        return []
    return [r for r in ledger.read(etype="out.close")
            if r.get("source") == source and r.get("source_id") == source_id]


def ingest_message(cfg, ledger, msg, keep_text=False):
    """Commit, route, and resolve one source message without guessing.

    A valid source ID is committed exactly once as ``qa.inbound`` before any
    routing side effect. A crash after receipt but before routing can be
    repaired by replay; per-effect source IDs prevent duplicate acts/findings.
    Successful routes are ignored on replay, while the two failures current
    config can repair may be retried. Full transactional indexing and
    authoritative host identity remain PORT-002/003 work.
    """
    if not isinstance(msg, dict):
        raise SchemaError("ingest message must be a JSON object")

    source = (cfg.transport.get("kind") if cfg else None) or "discord"
    source_id = _native_id(msg.get("id"))
    source_ts = msg.get("ts")
    timestamp_valid = (source_ts is None
                       or (not isinstance(source_ts, bool)
                           and isinstance(source_ts, (int, float))
                           and math.isfinite(source_ts) and source_ts >= 0))
    ts = source_ts if timestamp_valid else None
    identity = _identity(cfg, msg)
    relay = attribute_relay(cfg, msg)
    raw_content = msg.get("content", msg.get("text", ""))
    content_valid = isinstance(raw_content, str)
    content = raw_content if content_valid else ""
    text = relay.get("text") if relay["is_relay"] else content
    text = text or ""
    seat = relay.get("seat") if relay["is_relay"] else identity["seat"]
    sid = seat.id if seat else "unknown"
    relay_reason = relay["reason"]
    identity_source = ((relay_reason if relay_reason.startswith("relay_")
                        else f"relay_{relay_reason}")
                       if relay["is_relay"] else identity["identity_source"])
    role = ("relay" if relay["is_relay"] and identity["is_bot"] is True
            else identity["role"])

    # Do not replay legacy rows created before qa.route existed. Treating an
    # old receipt as unfinished could resolve a newly opened obligation with
    # historical text after an upgrade.
    if source_id:
        legacy = next((r for r in ledger.read(etype="qa.inbound")
                       if r.get("msg_id") == source_id and "source" not in r), None)
        if legacy is not None:
            return []

    receipt_fields = dict(
        ts=ts, seat=sid, chars=len(text), words=len(text.split()),
        msg_id=source_id, source=source, source_id=source_id,
        principal_id=identity["principal_id"], source_role=role,
        identity_source=identity_source, is_bot=identity["is_bot"],
        source_fingerprint=_source_fingerprint(msg),
        source_timestamp_invalid=(True if not timestamp_valid else None),
        via=(identity["author"] if relay["is_relay"] else None),
        text=(text[:400] if (keep_text or relay["is_relay"]) and text else None))
    if source_id:
        receipt, receipt_created = ledger.append_once(
            "qa.inbound", {"source": source, "source_id": source_id},
            **receipt_fields)
    else:
        receipt = ledger.append("qa.inbound", **receipt_fields)
        receipt_created = True
    written = [receipt] if receipt_created else []

    history = _route_history(ledger, source, source_id)

    def finish(status, reason, **fields):
        rec = _append_route(ledger, source, source_id, status, reason,
                            ts=ts, seat=sid, **fields)
        if rec is not None:
            written.append(rec)
        return written

    def recover_resolution():
        """Repair a receipt/effect/close crash window or a concurrent replay."""
        resolutions = _existing_resolution(ledger, source, source_id)
        if len(resolutions) > 1:
            return finish("quarantined", "multiple_source_resolutions",
                          repairable=False)
        if resolutions:
            return finish("routed", "resolution_recovered",
                          pair_id=resolutions[0]["id"])
        return None

    if (not receipt_created and receipt.get("source_fingerprint")
            and receipt_fields.get("source_fingerprint")
            and receipt["source_fingerprint"]
            != receipt_fields["source_fingerprint"]):
        return finish("quarantined", "source_payload_mismatch", repairable=False)
    if not receipt_created and history:
        latest = history[-1]
        if (latest.get("status") != "quarantined"
                or latest.get("reason") not in REPAIRABLE_ROUTE_REASONS):
            return []

    if not source_id:
        return finish("quarantined", "missing_source_id", repairable=False)
    if not timestamp_valid:
        return finish("quarantined", "invalid_timestamp", repairable=False)
    if not content_valid:
        return finish("quarantined", "invalid_content", repairable=False)

    if "pair_id" in msg and "correlation_id" in msg:
        return finish("quarantined", "duplicate_correlation_fields",
                      repairable=False)
    correlation_raw = (msg.get("pair_id") if "pair_id" in msg
                       else msg.get("correlation_id"))
    correlation_id = _native_id(correlation_raw)
    if correlation_raw is not None and correlation_id is None:
        return finish("quarantined", "invalid_correlation", repairable=False)

    prior_resolutions = _existing_resolution(ledger, source, source_id)
    if len(prior_resolutions) > 1:
        return finish("quarantined", "multiple_source_resolutions", repairable=False)
    if prior_resolutions:
        return finish("routed", "resolution_recovered",
                      pair_id=prior_resolutions[0]["id"])

    if relay["is_relay"]:
        if relay.get("seat") is None:
            reason = relay["reason"]
            return finish("quarantined", reason,
                          repairable=reason == "relay_unattributed")
        roll = parse_relay_roll(msg)
        if roll["total"] is None:
            return finish("quarantined", "relay_missing_total", repairable=False)
        if not roll["valid"]:
            return finish("quarantined", roll["validation"], repairable=False)

        actor = seat.display
        act = _append_effect_once(
            ledger, "act", source, source_id, ts=ts, actor=actor,
            text=f"{roll['label'] or 'roll'}: {roll['total']}"
                 + (f" ({roll['breakdown']})" if roll["breakdown"] else ""),
            roll_total=roll["total"], roll_label=roll["label"],
            roll_check=roll["check"], roll_natural=roll["natural"],
            roll_die=roll["die"], roll_provenance="relay_observed",
            roll_confidence="source_observed",
            sheet_modifier=roll["modifier"], sheet_id=seat.sheet_id,
            via=identity["author"], principal_id=identity["principal_id"])
        if act is not None:
            written.append(act)

        if roll["check"] and roll["modifier"] is not None:
            for prev in ledger.read(etype="act"):
                if (prev.get("source_id") != source_id
                        and prev.get("roll_check") == roll["check"]
                        and prev.get("actor") == actor
                        and isinstance(prev.get("sheet_modifier"), int)
                        and not isinstance(prev.get("sheet_modifier"), bool)
                        and prev["sheet_modifier"] != roll["modifier"]):
                    delta = _append_effect_once(
                        ledger, "qa.delta", source, source_id, ts=ts,
                        topic="modifier_drift", seat=sid, check=roll["check"],
                        detail=f"{roll['check']} was {prev['sheet_modifier']:+d} "
                               f"earlier and {roll['modifier']:+d} now",
                        observed=roll["modifier"],
                        previous=prev["sheet_modifier"])
                    if delta is not None:
                        written.append(delta)
                    break
        try:
            close = pairs.close_one(
                ledger, ("roll",), sid, {"roll": "consumed"},
                correlation_id=correlation_id, detail="typed relay result",
                ts=ts, source=source, source_id=source_id,
                evidence={"roll_total": roll["total"],
                          "roll_natural": roll["natural"],
                          "roll_die": roll["die"],
                          "roll_confidence": "source_observed",
                          "roll_provenance": "relay_observed",
                          "principal_id": identity["principal_id"]})
        except pairs.PairError as error:
            recovered = recover_resolution()
            if recovered is not None:
                return recovered
            return finish("quarantined", error.code, repairable=False,
                          detail=str(error))
        if close is None:
            recovered = recover_resolution()
            if recovered is not None:
                return recovered
            return finish("observed", "relay_roll_no_obligation")
        written.append(close)
        return finish("routed", "relay_roll_resolved", pair_id=close["id"])

    if identity["seat"] is None:
        return finish("quarantined", identity["reason"] or "unknown_principal",
                      repairable=identity["reason"] is None)
    if cfg and identity["seat"] is cfg.gm:
        return finish("ignored", "gm_echo")
    if not content.strip():
        return finish("ignored", "blank_content")

    try:
        close = pairs.close_one(
            ledger, ("cue", "checkin"), sid,
            {"cue": "taken", "checkin": "returned"},
            correlation_id=correlation_id, detail="non-empty inbound",
            ts=ts, source=source, source_id=source_id)
    except pairs.PairError as error:
        recovered = recover_resolution()
        if recovered is not None:
            return recovered
        return finish("quarantined", error.code, repairable=False,
                      detail=str(error))
    if close is not None:
        written.append(close)
        return finish("routed", "obligation_resolved", pair_id=close["id"])
    recovered = recover_resolution()
    if recovered is not None:
        return recovered

    # Prose is advisory even when it is an unambiguous number. It may produce
    # one attention item but never an act or a roll resolution.
    open_rolls = [p for p in pairs.open_now(ledger, "roll")
                  if p.get("seat") == sid]
    detected = detect_typed_roll(content) if open_rolls else {
        "total": None, "confidence": "none", "reason": "no_open_roll"}
    if detected["confidence"] in ("high", "low", "invalid"):
        label = ("implausible" if detected["confidence"] == "invalid"
                 else "possible")
        finding = _append_effect_once(
            ledger, "qc.finding", source, source_id, ts=ts,
            check="roll_result_advisory", severity="attention", seat=sid,
            detail=f"{identity['seat'].display} posted a {label} prose roll "
                   f"candidate ({detected['total']}); correlate and confirm it "
                   "before resolving a roll",
            evidence=(content[:120] if keep_text else None),
            candidate_total=detected["total"],
            confidence=detected["confidence"], provenance="prose_advisory",
            reason=detected["reason"])
        if finding is not None:
            written.append(finding)
        return finish("advisory", "prose_roll_candidate")
    return finish("observed", "no_compatible_obligation")


def summarize(cfg, ledger, msg, events):
    """One compact line per inbound message, for a human or a monitor.

    A recorder that writes to a file and says nothing is only half the job:
    it makes the evening auditable afterwards without making anyone aware of
    it now. This is the other half — the line that can become a notification,
    so the GM finds out a seat spoke without having to think to go and look.
    """
    if not events:
        return None
    inbound = next((e for e in events if e["type"] == "qa.inbound"), None)
    route = next((e for e in reversed(events) if e["type"] == "qa.route"), None)
    # A repair replay intentionally has no second receipt. Recover it from the
    # durable source key so the operator still sees the changed disposition.
    if not inbound and route and route.get("source_id"):
        inbound = next((e for e in reversed(ledger.read(etype="qa.inbound"))
                        if e.get("source") == route.get("source")
                        and e.get("source_id") == route.get("source_id")), None)
    if not inbound:
        return None
    seat = (route.get("seat") if route
            and route.get("status") != "quarantined"
            and route.get("seat") not in (None, "unknown")
            else inbound.get("seat"))
    disp = seat
    if cfg:
        s = cfg.seat(seat)
        if s:
            disp = s.display
    roll = next((e for e in events if e.get("roll_total") is not None), None)
    if roll:
        return (f"{disp} rolled {roll.get('roll_check') or 'a die'}: "
                f"{roll['roll_total']}"
                + (f" [{roll['via']}]" if roll.get("via") else ""))
    if route and route.get("status") == "quarantined":
        return (f"[quarantined:{route.get('reason', 'unknown')}] "
                f"{route.get('source', 'source')} "
                f"{route.get('source_id', 'without-id')}")
    if route and route.get("status") == "advisory":
        return (f"[advisory:{route.get('reason', 'review')}] {disp} — "
                "correlate explicitly before resolving")
    if route and route.get("status") == "ignored":
        return None
    raw_text = msg.get("content")
    text = (raw_text if isinstance(raw_text, str) else "")
    text = text.strip().replace("\n", " ")
    if len(text) > 240:
        text = text[:237] + "..."
    return f"{disp}: {text}" if text else f"{disp} posted something with no text"


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
            try:
                evs = ingest_message(cfg, ledger, msg, keep_text)
            except (SchemaError, pairs.PairError) as error:
                # One malformed or lifecycle-invalid message must be visible,
                # but must not detach a long-running listener from the table.
                source_id = (_native_id(msg.get("id"))
                             if isinstance(msg, dict) else None)
                print(f"ingest: rejected {source_id or 'message-without-id'}: "
                      f"{error}", file=sys.stderr, flush=True)
                continue
            line = summarize(cfg, ledger, msg, evs)
            if line:
                # stdout is the event stream: one line per thing the GM needs
                # to know about. Flushed immediately — a notification that
                # arrives when the buffer fills is not a notification.
                print(line, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        ledger.append("qa.listener", state="down", detail="ingest detached")
    return 0


if __name__ == "__main__":
    sys.exit(main())
