"""Mirror a game engine's log without losing or inventing continuity.

The engine log is authoritative about what happened.  A source cursor is not:
``log_len=10`` proves that ten entries exist, but a four-entry ``log_tail``
does not make the other six available.  The tap therefore advances only over
contiguous entries it actually appended to the session ledger.  Missing,
rewritten, reordered, reset, or unverifiable history fails with a typed
``EngineSyncError`` before the source cursor advances.

Ingestion is also deliberately separate from narration.  Mirroring an engine
entry proves only that the kit observed it.  ``acknowledge_narration`` advances
the narration cursor only when given an exact event returned by ``tap``; the
event's source index and content fingerprint are checked against the ledger.
"""

import hashlib
import json
import subprocess


SYNC_VERSION = 1
_ENGINE_TYPES = ("act", "event")


class EngineSyncError(RuntimeError):
    """A typed refusal to guess across missing or contradictory engine state."""

    def __init__(self, code, message, **context):
        super().__init__(message)
        self.code = code
        self.context = context

    def as_dict(self):
        return {"code": self.code, "message": str(self), **self.context}


def _refuse(code, message, **context):
    raise EngineSyncError(code, message, **context)


def _line_text(line):
    if isinstance(line, str):
        return line
    try:
        return json.dumps(line, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        _refuse("invalid_state", f"engine log entry is not JSON serializable: {exc}")


def _fingerprint_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _state_entries(state):
    """Return ``(total, start, entries, source_id)`` for a full log or tail."""
    if not isinstance(state, dict):
        _refuse("invalid_state", "engine state must be an object")

    full = "log" in state
    raw = state.get("log") if full else state.get("log_tail", [])
    raw = [] if raw is None else raw
    if not isinstance(raw, list):
        _refuse("invalid_state", "engine log/log_tail must be an array")

    total = state.get("log_len", len(raw))
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        _refuse("invalid_state", "engine log_len must be a non-negative integer",
                log_len=total)
    if len(raw) > total:
        _refuse("invalid_state", "engine state contains more entries than log_len",
                log_len=total, available=len(raw))
    if full and len(raw) != total:
        _refuse("invalid_state", "engine state labels an incomplete array as log",
                log_len=total, available=len(raw))

    start = total - len(raw) + 1
    entries = []
    for offset, line in enumerate(raw):
        text = _line_text(line)
        entries.append((start + offset, text, _fingerprint_text(text)))

    source_id = state.get("source_id")
    if source_id is None:
        source_id = state.get("match_id")
    if source_id is not None and (not isinstance(source_id, str) or not source_id.strip()):
        _refuse("invalid_state", "engine source_id must be a non-empty string",
                source_id=source_id)
    return total, start, entries, source_id


def _local_position(ledger):
    """Return the contiguous durable local cursor and its correlation records."""
    records = {}
    sources = set()
    for rec in ledger.read():
        if rec.get("type") not in _ENGINE_TYPES or "engine_log_index" not in rec:
            continue
        idx = rec.get("engine_log_index")
        fingerprint = rec.get("engine_log_fingerprint")
        text = rec.get("text")
        if (isinstance(idx, bool) or not isinstance(idx, int) or idx < 1
                or not isinstance(fingerprint, str) or not isinstance(text, str)
                or fingerprint != _fingerprint_text(text)):
            _refuse("local_cursor_invalid", "ledger contains invalid engine correlation data",
                    engine_log_index=idx)
        previous = records.get(idx)
        if previous and previous.get("engine_log_fingerprint") != fingerprint:
            _refuse("local_fork", "ledger contains conflicting entries at one engine index",
                    engine_log_index=idx)
        if previous:
            _refuse("local_duplicate", "ledger contains a duplicate engine index",
                    engine_log_index=idx)
        records[idx] = rec
        rec_source = rec.get("engine_source_id")
        if rec_source is not None:
            if not isinstance(rec_source, str) or not rec_source.strip():
                _refuse("local_cursor_invalid", "ledger contains an invalid engine source",
                        engine_source_id=rec_source)
            sources.add(rec_source)

    ordered = sorted(records)
    for expected, observed in enumerate(ordered, 1):
        if expected != observed:
            _refuse("local_gap", "ledger engine history is not contiguous",
                    first_missing=expected, synced_through=ordered[-1])
    have = ordered[-1] if ordered else 0

    marks = ledger.read(etype="qc.mark")
    for mark in marks:
        if "engine_log_len" not in mark:
            continue
        mark_len = mark["engine_log_len"]
        if isinstance(mark_len, bool) or not isinstance(mark_len, int) or mark_len < 0:
            _refuse("local_cursor_invalid", "ledger contains an invalid engine cursor mark",
                    engine_log_len=mark_len)
    legacy = [m for m in marks if m.get("engine_log_len", 0)
              and m.get("engine_sync_version") != SYNC_VERSION]
    if legacy:
        _refuse(
            "legacy_cursor_unverifiable",
            "legacy engine cursor has no per-entry correlation; start a new ledger or migrate it",
            engine_log_len=max(m.get("engine_log_len", 0) for m in legacy),
        )

    sync_marks = [m for m in marks if m.get("mark_kind") == "engine_sync"
                  and m.get("engine_sync_version") == SYNC_VERSION]
    marked = max((m.get("engine_log_len", 0) for m in sync_marks), default=0)
    if marked > have:
        _refuse("local_cursor_ahead", "ledger cursor advances beyond durable engine entries",
                cursor=marked, durable_entries=have)
    for mark in marks:
        mark_source = mark.get("engine_source_id")
        if mark.get("engine_sync_version") == SYNC_VERSION and mark_source is not None:
            if not isinstance(mark_source, str) or not mark_source.strip():
                _refuse("local_cursor_invalid", "ledger contains an invalid engine source",
                        engine_source_id=mark_source)
            sources.add(mark_source)
    if len(sources) > 1:
        _refuse("local_source_conflict", "ledger contains more than one engine source",
                source_ids=sorted(sources))
    return have, records, (next(iter(sources)) if sources else None), marked


def synced_through(ledger):
    """Highest contiguous engine index durably represented in the ledger."""
    return _local_position(ledger)[0]


def narrated_through(ledger):
    """Highest engine index covered by a valid correlated narration acknowledgment."""
    _have, records, _source, _marked = _local_position(ledger)
    acknowledged = _narration_indexes(ledger, records)
    narrated = 0
    while narrated + 1 in acknowledged:
        narrated += 1
    return narrated


def _narration_indexes(ledger, records):
    """Validate narration marks and return the indexes they acknowledge."""
    acknowledged = set()
    for mark in ledger.read(etype="qc.mark"):
        if mark.get("mark_kind") != "narration_ack":
            continue
        idx = mark.get("ack_engine_log_index")
        fingerprint = mark.get("ack_engine_log_fingerprint")
        if isinstance(idx, bool) or not isinstance(idx, int) or idx < 1:
            _refuse("narration_ack_invalid",
                    "narration acknowledgment has an invalid engine index",
                    engine_log_index=idx)
        rec = records.get(idx)
        if not rec or rec.get("engine_log_fingerprint") != fingerprint:
            _refuse("narration_ack_invalid",
                    "narration acknowledgment does not match a durable engine event",
                    engine_log_index=idx)
        acknowledged.add(idx)
    return acknowledged


def _mismatch_code(entries, records, mismatched):
    available = {fingerprint: idx for idx, _text, fingerprint in entries}
    for idx in mismatched:
        old = records[idx]["engine_log_fingerprint"]
        if old in available and available[old] != idx:
            return "source_reordered"
    return "source_fork"


def _turn_already_recorded(ledger, total, source_id):
    return any(r.get("type") == "turn" and r.get("engine_log_len") == total
               and r.get("engine_source_id") in (None, source_id)
               for r in ledger.read())


def _append_sync_mark(ledger, total, source_id):
    return ledger.append(
        "qc.mark",
        narrated_through=narrated_through(ledger),
        engine_log_len=total,
        engine_sync_version=SYNC_VERSION,
        mark_kind="engine_sync",
        engine_source_id=source_id,
    )


def tap(ledger, state):
    """Mirror a complete contiguous engine delta and return play events written.

    ``state`` may carry a complete ``log`` or a suffix ``log_tail``.  A suffix
    is accepted only when it contains the next expected index.  Contradictory,
    reset, forked, reordered, compacted, or cursor-ahead state raises
    :class:`EngineSyncError` without advancing the cursor.
    """
    total, start, entries, state_source = _state_entries(state)
    have, records, local_source, marked = _local_position(ledger)

    if local_source and state_source is None:
        _refuse("source_unverifiable",
                "engine state omitted the source_id already bound to this session",
                expected_source=local_source)
    if local_source and local_source != state_source:
        _refuse("source_fork", "engine source changed for an existing session",
                expected_source=local_source, observed_source=state_source)
    source_id = state_source or local_source

    if have and total == 0:
        _refuse("source_reset", "engine log reset behind the durable cursor",
                cursor=have, log_len=total)
    if total < have:
        _refuse("cursor_ahead", "durable cursor is ahead of the engine state",
                cursor=have, log_len=total)

    by_index = {idx: (text, fingerprint) for idx, text, fingerprint in entries}
    mismatched = [idx for idx in by_index if idx <= have
                  and records[idx]["engine_log_fingerprint"] != by_index[idx][1]]
    if mismatched:
        code = _mismatch_code(entries, records, mismatched)
        _refuse(code, "engine history changed at an already ingested index",
                first_mismatch=min(mismatched), cursor=have, log_len=total)

    expected = have + 1
    if total > have and expected not in by_index:
        _refuse("source_gap", "engine tail does not contain the next expected entry",
                cursor=have, log_len=total, available_from=start,
                available=len(entries), first_missing=expected)
    if total == have and total and not any(idx <= have for idx in by_index):
        _refuse("source_unverifiable", "engine state has no overlap with the durable cursor",
                cursor=have, log_len=total, available=len(entries))

    written = []
    for idx in range(expected, total + 1):
        text, fingerprint = by_index[idx]
        etype = "event" if "[ruling" in text else "act"
        actor = (text.split("] ", 1)[-1].split("→")[0].split(":")[0]
                 .replace("[ruling.", "").strip())
        written.append(ledger.append(
            etype,
            actor=actor or "engine",
            text=text,
            engine_log_index=idx,
            engine_log_fingerprint=fingerprint,
            engine_source_id=source_id,
        ))

    # A failed mark append is recoverable: the durable correlated play entries
    # are the cursor.  A retry writes only the missing mark (and turn, if any).
    needs_mark = total > marked or (state_source is not None and local_source is None)
    if needs_mark:
        turn = state.get("turn")
        units = state.get("units") or {}
        if turn and units.get(turn) and not _turn_already_recorded(ledger, total, source_id):
            name = str(units[turn]).split(" [")[0]
            written.append(ledger.append(
                "turn", actor=name, engine_log_len=total,
                engine_source_id=source_id,
            ))
        _append_sync_mark(ledger, total, source_id)
    return written


def acknowledge_narration(ledger, event, evidence=None):
    """Acknowledge narration through one exact engine event returned by ``tap``.

    The event's source index and fingerprint must still match the durable
    ledger.  Repeating or replaying an older valid acknowledgment is a no-op.
    """
    if not isinstance(event, dict):
        _refuse("narration_ack_invalid", "narration acknowledgment needs an event object")
    idx = event.get("engine_log_index")
    fingerprint = event.get("engine_log_fingerprint")
    if isinstance(idx, bool) or not isinstance(idx, int) or idx < 1:
        _refuse("narration_ack_invalid",
                "narration acknowledgment has an invalid engine index",
                engine_log_index=idx)
    have, records, source_id, _marked = _local_position(ledger)
    if idx > have:
        _refuse("narration_ahead", "cannot narrate beyond durable engine history",
                narrated_through=idx, synced_through=have)
    rec = records.get(idx)
    correlation_fields = (
        "type", "actor", "text", "engine_log_index",
        "engine_log_fingerprint", "engine_source_id",
    )
    if (not rec or not fingerprint
            or any(event.get(field) != rec.get(field) for field in correlation_fields)):
        _refuse("narration_ack_mismatch",
                "narration acknowledgment does not match a durable engine event",
                engine_log_index=idx)
    acknowledged = _narration_indexes(ledger, records)
    if idx in acknowledged:
        return []
    acknowledged.add(idx)
    through = 0
    while through + 1 in acknowledged:
        through += 1
    mark = ledger.append(
        "qc.mark",
        narrated_through=through,
        engine_log_len=have,
        engine_sync_version=SYNC_VERSION,
        mark_kind="narration_ack",
        engine_source_id=source_id,
        ack_engine_log_index=idx,
        ack_engine_log_fingerprint=fingerprint,
        evidence=evidence,
    )
    return [mark]


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
