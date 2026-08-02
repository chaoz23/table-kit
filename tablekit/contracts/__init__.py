"""Packaged, read-only access to the suite contract artifacts."""

import json
from importlib import resources


SCHEMAS = {
    "evaluation": "table-evaluation-1.0.schema.json",
    "event": "table-event-1.0.schema.json",
}
GOLDEN_SESSION = "golden-session-1.0.jsonl"


def _text(name):
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")


def schema(name):
    """Return a fresh parsed schema by stable short name."""
    try:
        filename = SCHEMAS[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown contract {name!r}; choose {', '.join(sorted(SCHEMAS))}"
        ) from exc
    return json.loads(_text(filename))


def golden_session():
    """Return the normative ordered TableEvent interoperability fixture."""
    return [json.loads(line) for line in _text(GOLDEN_SESSION).splitlines()
            if line.strip()]


def evaluation_semantic_errors(value):
    """Return cross-field coverage errors JSON Schema cannot express."""
    errors = []
    coverage = value.get("coverage") if isinstance(value, dict) else None
    if not isinstance(coverage, dict):
        return ["coverage must be an object"]
    counts = {name: coverage.get(name)
              for name in ("input", "compatible", "eligible", "evaluated", "skipped")}
    if all(isinstance(item, int) and not isinstance(item, bool)
           for item in counts.values()):
        if counts["compatible"] > counts["input"]:
            errors.append("compatible exceeds input")
        if counts["eligible"] > counts["compatible"]:
            errors.append("eligible exceeds compatible")
        if counts["evaluated"] + counts["skipped"] != counts["eligible"]:
            errors.append("evaluated plus skipped must equal eligible")
    complete = value.get("status") in {
        "checked_clean", "checked_with_advisories", "findings"
    }
    if complete and coverage.get("evidence_required") and not coverage.get("eligible"):
        errors.append("required evidence has zero eligible inputs")
    for evaluator in coverage.get("evaluators", []):
        if not isinstance(evaluator, dict):
            errors.append("evaluator coverage must be an object")
            continue
        eligible = evaluator.get("eligible")
        evaluated = evaluator.get("evaluated")
        skipped = evaluator.get("skipped")
        if all(isinstance(item, int) and not isinstance(item, bool)
               for item in (eligible, evaluated, skipped)):
            if evaluated + skipped != eligible:
                errors.append(f"{evaluator.get('id')}: evaluated plus skipped must equal eligible")
        if complete and evaluator.get("required") and evaluator.get("status") not in {
                "evaluated", "not_applicable"}:
            errors.append(f"{evaluator.get('id')}: required evaluator is incomplete")
    return errors


def require_evaluation_semantics(value):
    """Raise ValueError when an envelope's cross-field counts are incoherent."""
    errors = evaluation_semantic_errors(value)
    if errors:
        raise ValueError("; ".join(errors))
    return value


def event_stream_errors(events, expected_start=1):
    """Return identity/order errors for one canonical session stream page."""
    errors = []
    event_ids = set()
    session = campaign = None
    expected = expected_start
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append(f"index {index}: event must be an object")
            continue
        event_id = event.get("event_id")
        if event_id in event_ids:
            errors.append(f"index {index}: duplicate event_id {event_id!r}")
        event_ids.add(event_id)
        if session is None:
            session, campaign = event.get("session_id"), event.get("campaign_id")
        elif (event.get("session_id"), event.get("campaign_id")) != (session, campaign):
            errors.append(f"index {index}: mixed campaign/session stream")
        sequence = event.get("session_sequence")
        if sequence != expected:
            errors.append(f"index {index}: expected session_sequence {expected}, got {sequence!r}")
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                expected = sequence
        expected += 1
    return errors


def require_event_stream(events, expected_start=1):
    """Raise ValueError on duplicate, out-of-order, missing, or mixed events."""
    errors = event_stream_errors(events, expected_start=expected_start)
    if errors:
        raise ValueError("; ".join(errors))
    return events
