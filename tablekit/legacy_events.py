"""Conservative migration from table-kit's legacy ledger to TableEvent v1.

Migration is a projection, not attestation.  It emits only ``self_attested``
events, derives deterministic migration IDs, and retains legacy native IDs as
source/correlation metadata rather than promoting them into authority.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json

from . import contracts
from .events import Ledger, SchemaError, validate


MIGRATION_SCHEMA_VERSION = "table.event.migration/1.0"
TABLE_EVENT_SCHEMA_VERSION = "table.event/1.0"
_SOURCE_KINDS = {"discord", "tablekit", "engine", "dmcheck",
                 "charactercheck", "srdcheck", "host", "fixture", "other"}
_PAIR_OBLIGATIONS = {"cue": "cue", "roll": "consume_roll",
                     "checkin": "answer", "endmarker": "narrate_event"}


class LegacyMigrationError(ValueError):
    """A stable refusal to invent meaning across incompatible legacy data."""

    def __init__(self, code, message, line=None):
        self.code = code
        self.line = line
        super().__init__(message)

    def to_dict(self):
        result = {"code": self.code, "message": str(self)}
        if self.line is not None:
            result["line"] = self.line
        return result


@dataclass
class LegacyMigration:
    campaign_id: str
    session_id: str
    source_instance: str
    input_count: int
    events: list = field(default_factory=list)
    skipped: list = field(default_factory=list)

    def summary(self):
        payload = "".join(
            json.dumps(event, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n"
            for event in self.events).encode("utf-8")
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "status": "migrated",
            "authority_status": "self_attested",
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "input_count": self.input_count,
            "compatible_count": len(self.events),
            "skipped_count": len(self.skipped),
            "skipped": list(self.skipped),
            "event_schema": TABLE_EVENT_SCHEMA_VERSION,
            "output_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        }

    def jsonl(self):
        return "".join(json.dumps(event, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")) + "\n"
                       for event in self.events)


def _identifier(value, field):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise LegacyMigrationError(
            "legacy_migration.invalid_id",
            "%s must be a nonempty string of at most 256 characters" % field)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LegacyMigrationError(
            "legacy_migration.invalid_id",
            "%s must contain valid Unicode" % field) from exc
    return value


def _timestamp(value):
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z")
    except (OSError, OverflowError, ValueError) as exc:
        raise LegacyMigrationError(
            "legacy_migration.timestamp_range",
            "legacy timestamp is outside the TableEvent date-time range") from exc


def _canonical_digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _event_id(campaign_id, session_id, line, row, variant="primary"):
    material = {"campaign_id": campaign_id, "session_id": session_id,
                "legacy_line": line, "legacy_row": row, "variant": variant}
    return "legacy-" + _canonical_digest(material)[:40]


def _native_id(row):
    for key in ("source_id", "msg_id", "message_id"):
        value = row.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            value = str(value).strip()
            if value:
                return value[:256]
    index = row.get("engine_log_index")
    if isinstance(index, int) and not isinstance(index, bool) and index >= 1:
        source = row.get("engine_source_id")
        prefix = source.strip() if isinstance(source, str) and source.strip() else "engine"
        return ("%s:%d" % (prefix, index))[:256]
    return None


def _correlations(row):
    values = []
    for key in ("correlation_id", "pair_id", "id", "operation_id",
                "source_id", "msg_id", "message_id"):
        value = row.get(key)
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, (str, int)) and not isinstance(candidate, bool):
                candidate = str(candidate).strip()
                if candidate and len(candidate) <= 256 and candidate not in values:
                    values.append(candidate)
    return values


def _explicit_obligations(row):
    values = []
    for key in ("obligation_id", "correlation_id", "pair_id"):
        value = row.get(key)
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate and len(candidate) <= 256:
                if candidate not in values:
                    values.append(candidate)
    return values


def _principal(row, default="tablekit"):
    actor = None
    for key in ("principal_id", "actor", "seat"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            actor = value.strip()[:256]
            break
    identity = actor or default
    return {"id": identity, "actor_id": actor, "controller_id": None,
            "role": "unknown" if actor else "system"}


def _source(row, source_instance):
    original = row.get("source")
    kind = original if original in _SOURCE_KINDS else "tablekit"
    if "engine_log_index" in row:
        kind = "engine"
    sequence = row.get("engine_log_index")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        sequence = None
    return {"kind": kind, "instance": source_instance,
            "native_id": _native_id(row), "sequence": sequence,
            "attestation": "self_attested"}


def _base(row, line, sequence, campaign_id, session_id, source_instance,
          event_type, payload, provenance, audience=None, visibility="table",
          causation_id=None, variant="primary"):
    event_id = _event_id(campaign_id, session_id, line, row, variant=variant)
    return {
        "schema_version": TABLE_EVENT_SCHEMA_VERSION,
        "event_id": event_id,
        "campaign_id": campaign_id,
        "session_id": session_id,
        "session_sequence": sequence,
        "source": _source(row, source_instance),
        "occurred_at": _timestamp(row["ts"]),
        # The legacy ledger has one timestamp. Reusing it is explicit
        # migration provenance, not a claim about a separately observed time.
        "recorded_at": _timestamp(row["ts"]),
        "principal": _principal(row),
        "event_type": event_type,
        "payload": payload,
        "correlation_ids": _correlations(row),
        "causation_id": causation_id,
        "audience": audience or ["table"],
        "visibility": visibility,
        "sensitivity": "normal",
        "provenance": provenance,
        "integrity": {"predecessor_digest": None, "event_digest": None,
                      "checkpoint": "same_writer"},
    }


def _map_row(row, line, sequence, campaign_id, session_id, source_instance,
             context):
    kind = row["type"]
    args = (row, line, sequence, campaign_id, session_id, source_instance)
    if kind == "turn":
        return _base(*args, "turn.started", {"actor_id": row["actor"]}, "decided")
    if kind == "ux.turn":
        return _base(*args, "turn.started", {"actor_id": row["seat"]}, "observed")
    if kind == "act" and "roll_total" in row:
        if (not isinstance(row.get("roll_total"), (int, float))
                or isinstance(row.get("roll_total"), bool)):
            raise LegacyMigrationError(
                "legacy_migration.invalid_roll",
                "roll_total must be a finite number", line=line)
        natural = row.get("roll_natural")
        if not isinstance(natural, int) or isinstance(natural, bool) or natural < 1:
            natural = None
        roll_kind = row.get("roll_check") or row.get("roll_label") or "legacy-roll"
        roll = _base(*args, "roll.observed",
                     {"total": row["roll_total"], "natural": natural,
                      "roll_kind": str(roll_kind)}, "observed")
        pair_id = context["roll_pair_by_source"].get(row.get("source_id"))
        if pair_id is None:
            return roll
        obligation = _base(
            row, line, sequence + 1, campaign_id, session_id, source_instance,
            "narration.obligation",
            {"obligation_id": pair_id, "kind": "consume_roll"}, "derived",
            audience=["operator"], visibility="system",
            causation_id=roll["event_id"], variant="consume-roll-obligation")
        return [roll, obligation]
    if kind == "act":
        return _base(*args, "action.declared",
                     {"action_kind": row["text"], "legal_timing": "unknown"},
                     "reported")
    if kind == "event" and "engine_log_index" in row:
        _event, obligation_id = context["engine_obligations"][line]
        return _base(*args, "narration.obligation",
                     {"obligation_id": obligation_id, "kind": "narrate_event"},
                     "derived", audience=["operator"], visibility="system")
    if kind == "event" and isinstance(row.get("actor"), str) and row["actor"].strip():
        return _base(*args, "narration.observed",
                     {"content": row["text"],
                      "resolves_obligation_ids": _explicit_obligations(row)}, "observed")
    if kind == "event":
        obligation_id = "obligation-" + _event_id(
            campaign_id, session_id, line, row).split("legacy-", 1)[1]
        return _base(*args, "narration.obligation",
                     {"obligation_id": obligation_id, "kind": "narrate_event"},
                     "derived", audience=["operator"], visibility="system")
    if kind == "qa.inbound":
        redacted = not isinstance(row.get("text"), str)
        return _base(*args, "message.observed",
                     {"content": "" if redacted else row["text"],
                      "content_redacted": redacted},
                     "observed")
    if kind == "qa.post.receipt":
        return _base(*args, "delivery.received",
                     {"operation_id": row["operation_id"],
                      "message_id": row["message_id"], "status": "received"},
                     "observed", audience=["operator"], visibility="system")
    if kind == "qa.post.partial":
        return _base(*args, "transport.gap",
                     {"expected_sequence": row["chunks_planned"],
                      "observed_sequence": row["chunks_sent"],
                      "recoverable": True}, "observed",
                     audience=["operator"], visibility="system")
    if kind == "out.open":
        if row["pair"] == "roll":
            return None
        obligation_kind = _PAIR_OBLIGATIONS[row["pair"]]
        return _base(*args, "narration.obligation",
                     {"obligation_id": row["id"], "kind": obligation_kind},
                     "derived", audience=["operator"], visibility="system")
    if kind == "out.close" and row["pair"] == "roll" and row["outcome"] == "consumed":
        return _base(*args, "narration.observed",
                     {"content": "Legacy ledger records this roll as consumed.",
                      "resolves_obligation_ids": [row["id"]]}, "derived",
                     audience=["operator"], visibility="system",
                     causation_id=context["pair_obligation_events"].get(row["id"]))
    if kind == "qc.mark" and row.get("mark_kind") == "narration_ack":
        source = row.get("engine_source_id")
        index = row.get("ack_engine_log_index")
        target = context["engine_obligation_keys"].get((source, index))
        if target is None:
            raise LegacyMigrationError(
                "legacy_migration.orphan_narration_ack",
                "narration acknowledgment has no matching migrated engine event",
                line=line)
        obligation_event, obligation_id = target
        return _base(*args, "narration.observed",
                     {"content": "Legacy ledger records exact narration acknowledgment.",
                      "resolves_obligation_ids": [obligation_id]}, "derived",
                     audience=["operator"], visibility="system",
                     causation_id=obligation_event)
    return None


def migrate_records(records, campaign_id, session_id,
                    source_instance="tablekit-legacy-migration"):
    """Project validated audit records into one contiguous TableEvent stream."""
    campaign_id = _identifier(campaign_id, "campaign_id")
    session_id = _identifier(session_id, "session_id")
    source_instance = _identifier(source_instance, "source_instance")
    if not isinstance(records, list):
        raise LegacyMigrationError("legacy_migration.input_type",
                                   "records must be a list")
    result = LegacyMigration(campaign_id, session_id, source_instance,
                             input_count=len(records))
    context = {"engine_obligations": {}, "engine_obligation_keys": {},
               "pair_obligation_events": {}, "roll_pair_by_source": {}}
    roll_by_source = {}
    closes = {}
    for line, row in enumerate(records, 1):
        if not isinstance(row, dict) or row.get("type") == "_malformed":
            continue
        event_id = _event_id(campaign_id, session_id, line, row)
        if row.get("type") == "event" and "engine_log_index" in row:
            obligation_id = "obligation-" + event_id.split("legacy-", 1)[1]
            context["engine_obligations"][line] = (event_id, obligation_id)
            key = (row.get("engine_source_id"), row.get("engine_log_index"))
            context["engine_obligation_keys"][key] = (event_id, obligation_id)
        if row.get("type") == "out.close":
            closes[row.get("id")] = row
        if row.get("type") == "act" and "roll_total" in row:
            native = row.get("source_id")
            if isinstance(native, str) and native:
                roll_by_source[native] = (line, row, event_id)
    for pair_id, close in closes.items():
        if close.get("pair") != "roll":
            continue
        source_id = close.get("source_id")
        if source_id in roll_by_source:
            roll_line, roll_row, _roll_event = roll_by_source[source_id]
            context["roll_pair_by_source"][source_id] = pair_id
            context["pair_obligation_events"][pair_id] = _event_id(
                campaign_id, session_id, roll_line, roll_row,
                variant="consume-roll-obligation")
    for line, row in enumerate(records, 1):
        if not isinstance(row, dict):
            raise LegacyMigrationError("legacy_migration.invalid_row",
                                       "legacy row must be an object", line=line)
        if row.get("type") == "_malformed":
            raise LegacyMigrationError(
                "legacy_migration.malformed_ledger",
                "ledger contains a malformed row: %s" % row.get("error", "unknown error"),
                line=row.get("line", line))
        try:
            validate(row)
        except SchemaError as exc:
            raise LegacyMigrationError(
                "legacy_migration.invalid_row", str(exc), line=line) from exc
        event = _map_row(row, line, len(result.events) + 1, campaign_id,
                         session_id, source_instance, context)
        if event is None:
            reason = ("represented_by_roll_obligation"
                      if row.get("type") == "out.open"
                      and row.get("pair") == "roll"
                      and row.get("id") in context["pair_obligation_events"]
                      else "no_lossless_table_event_projection")
            result.skipped.append({"line": line, "type": row.get("type"),
                                   "reason": reason})
        elif isinstance(event, list):
            result.events.extend(event)
        else:
            result.events.append(event)
    if not result.events:
        raise LegacyMigrationError(
            "legacy_migration.zero_compatible",
            "ledger contains no row with a lossless TableEvent projection")
    contracts.require_event_stream(result.events)
    return result


def migrate_ledger(path, campaign_id, session_id,
                   source_instance="tablekit-legacy-migration"):
    """Read the audit surface so corruption cannot disappear during migration."""
    ledger = Ledger(path)
    return migrate_records(ledger.read(), campaign_id, session_id,
                           source_instance=source_instance)
