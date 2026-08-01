"""The event schema — one append-only stream for a live table.

A table run emits exactly ONE file. It is simultaneously:

  * the **play ledger** — `turn` / `act` / `event` records in the shape
    `dmcheck` already reads, so a conduct checker can be pointed straight at
    it with no export step; and
  * the **instrumentation stream** — four lanes of telemetry about the run
    itself, which `dmcheck` ignores because it does not recognise the types.

Keeping them in one file is the point. A separate telemetry file drifts from
the ledger the moment anything crashes, and then you are reconciling two
partial accounts of the same evening.

## The four instrumentation lanes

| lane  | question it answers            | source                   |
|-------|--------------------------------|--------------------------|
| `qa`  | did the machinery work?        | the kit, automatically   |
| `qc`  | was the refereeing correct?    | checks, automatically    |
| `ux`  | what was the seat's experience?| timings, automatically   |
| `uxr` | how did it *feel* from a seat? | **inferred + asked**     |

The split between `ux` and `uxr` is not cosmetic. Everything in `ux` is
derivable from the record afterwards. Nothing in `uxr` is: whether a beat
landed, whether a player was bored, whether a word was understood, and
whether someone got to do the thing their character is *for* leave no trace
in a transcript.

`uxr` records are inferred from what people already said, or asked for in
plain English at session close. **Players are never given a command syntax to
produce them** — see `tablekit.uxr` for why that is a hard product constraint
rather than a preference.

A fifth lane, `out`, records outcome PAIRS — an intent opened and later
closed with what actually happened. Those are the only records in the file
that can say whether a craft move worked.
"""

import json
import math
import os
import stat
import tempfile
import time

try:  # Unix provides the lock; O_APPEND remains the cross-platform baseline.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

LANES = ("play", "qa", "qc", "ux", "uxr", "out")

# Play types are fixed by dmcheck's ledger reader. Do not extend this tuple
# without checking `dmcheck.core.load_ledger` — an unrecognised play type is
# silently ignored there, which looks exactly like a table that never acted.
PLAY_TYPES = ("turn", "act", "event")

# type -> required keys (beyond ts/type). Unknown types are refused at write
# time: a typo that lands in the file as an unreadable record is worse than a
# crash, because it reads as an absence later.
SCHEMA = {
    # --- play (dmcheck-compatible) ------------------------------------
    "turn":  ("actor",),
    "act":   ("actor", "text"),
    "event": ("text",),
    # --- qa: does the machinery work ----------------------------------
    "qa.post":       ("ok", "chars", "chunks"),
    "qa.post_failed": ("error",),
    "qa.inbound":    ("seat", "chars"),
    "qa.listener":   ("state",),
    "qa.command":    ("cmd", "ok"),
    # Parked for below-the-table investigation. Never interrupts play, never
    # accuses anyone, never blocks. A discrepancy noticed mid-session is worth
    # exactly one line in a list and zero seconds of the evening.
    "qa.delta":      ("topic", "detail"),
    # --- qc: is the refereeing correct --------------------------------
    "qc.finding": ("check", "detail"),
    "qc.pass":    ("checks",),
    # Written ONLY by detector.record(), so "was this session checked?" cannot
    # be satisfied by a finding that some other code path happened to emit.
    "qc.run":     ("findings",),
    "qc.mark":    ("narrated_through",),
    # --- ux: objective seat experience --------------------------------
    "ux.beat":      ("words", "chunks"),
    "ux.turn":      ("seat", "wait_s"),
    "ux.seat_idle": ("seat", "idle_s"),
    # --- uxr: elicited, subjective ------------------------------------
    "uxr.signal":  ("seat", "signal", "quote", "source"),
    "uxr.debrief": ("seat", "question", "answer"),
    # --- out: outcome pairs (E5) --------------------------------------
    "out.open":  ("pair", "id"),
    "out.close": ("pair", "id", "outcome"),
}

# Pair kinds. Each is an intent whose payoff is observable, which is the whole
# bar for entry: if nobody can tell afterwards whether it worked, it is not a
# pair, it is an opinion.
PAIR_KINDS = {
    "cue":       "GM addressed a seat; did that seat act, and how fast?",
    "roll":      "GM called for a roll; was a result produced and consumed?",
    "checkin":   "a quiet seat was checked on; did it come back?",
    "endmarker": "a session end marker; did the next opening match it?",
}

PAIR_OUTCOMES = ("taken", "ignored", "expired", "superseded", "consumed",
                 "unconsumed", "returned", "absent", "matched", "diverged")

# A corrupt or hostile producer must not make every report allocate an
# unbounded string. Oversized rows are diagnosed exactly like malformed rows.
MAX_LINE_BYTES = 1024 * 1024


def _string(value, field, etype):
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{etype}: {field} must be a non-empty string")


def _integer(value, field, etype, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(f"{etype}: {field} must be an integer")
    if value < minimum:
        raise SchemaError(f"{etype}: {field} must be at least {minimum}")


def _number(value, field, etype, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{etype}: {field} must be a finite number")
    if not math.isfinite(value):
        raise SchemaError(f"{etype}: {field} must be a finite number")
    if value < minimum:
        raise SchemaError(f"{etype}: {field} must be at least {minimum}")


def _boolean(value, field, etype):
    if not isinstance(value, bool):
        raise SchemaError(f"{etype}: {field} must be a boolean")


def _json_value(value, path="event"):
    """Reject non-JSON and non-finite extension data before any write."""
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SchemaError(f"{path}: non-finite numbers are not valid JSON")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _json_value(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SchemaError(f"{path}: JSON object keys must be strings")
            _json_value(item, f"{path}.{key}")
        return
    raise SchemaError(f"{path}: {type(value).__name__} is not a JSON value")


def _invalid_json_constant(value):
    raise ValueError(f"invalid JSON constant {value}")


FIELD_VALIDATORS = {
    "turn": {"actor": _string},
    "act": {"actor": _string, "text": _string},
    "event": {"text": _string},
    "qa.post": {"ok": _boolean,
        "chars": _integer,
        "chunks": lambda v, f, t: _integer(v, f, t, 1)},
    "qa.post_failed": {"error": _string},
    "qa.inbound": {"seat": _string, "chars": _integer},
    "qa.listener": {"state": _string},
    "qa.command": {"cmd": _string, "ok": _boolean},
    "qa.delta": {"topic": _string, "detail": _string},
    "qc.finding": {"check": _string, "detail": _string},
    "qc.pass": {"checks": _integer},
    "qc.run": {"findings": _integer},
    "qc.mark": {"narrated_through": _integer},
    "ux.beat": {"words": _integer,
                 "chunks": lambda v, f, t: _integer(v, f, t, 1)},
    "ux.turn": {"seat": _string, "wait_s": _number},
    "ux.seat_idle": {"seat": _string, "idle_s": _number},
    "uxr.signal": {"seat": _string, "signal": _string, "quote": _string,
                   "source": _string},
    "uxr.debrief": {"seat": _string, "question": _string, "answer": _string},
    "out.open": {"pair": _string, "id": _string},
    "out.close": {"pair": _string, "id": _string, "outcome": _string},
}


class SchemaError(ValueError):
    """Refused at write time. See the module docstring for why this is loud."""


def lane_of(etype):
    return etype.split(".", 1)[0] if "." in etype else "play"


def validate(rec):
    """Return `rec` unchanged, or raise SchemaError.

    Strict about the registered type, timestamp, required fields, their types,
    domains, and JSON finiteness. Extra keys remain allowed: they are how a
    table adds source-native context without forking the schema.
    """
    if not isinstance(rec, dict):
        raise SchemaError("event row must be a JSON object")
    _json_value(rec)
    etype = rec.get("type")
    if not isinstance(etype, str):
        raise SchemaError("event type must be a non-empty string")
    if etype not in SCHEMA:
        raise SchemaError(
            f"unknown event type {etype!r}; known types: {', '.join(sorted(SCHEMA))}")
    ts = rec.get("ts")
    if (isinstance(ts, bool) or not isinstance(ts, (int, float))
            or not math.isfinite(ts) or ts < 0):
        raise SchemaError(f"{etype}: ts must be an epoch float")
    missing = [k for k in SCHEMA[etype] if k not in rec]
    if missing:
        raise SchemaError(f"{etype}: missing required key(s) {', '.join(missing)}")
    for field, validator in FIELD_VALIDATORS[etype].items():
        validator(rec[field], field, etype)
    if etype in ("out.open", "out.close") and rec["pair"] not in PAIR_KINDS:
        raise SchemaError(
            f"unknown pair kind {rec['pair']!r}; known: {', '.join(sorted(PAIR_KINDS))}")
    if etype == "out.close" and rec["outcome"] not in PAIR_OUTCOMES:
        raise SchemaError(
            f"unknown outcome {rec['outcome']!r}; known: {', '.join(PAIR_OUTCOMES)}")
    return rec


def make(etype, ts=None, **fields):
    """Build and validate one event."""
    when = time.time() if ts is None else ts
    if isinstance(when, bool) or not isinstance(when, (int, float)):
        raise SchemaError(f"{etype}: ts must be an epoch float")
    rec = {"ts": float(when), "type": etype}
    rec.update({k: v for k, v in fields.items() if v is not None})
    return validate(rec)


class Ledger:
    """Append-only JSONL. One per session; never rewritten in place.

    Each append is encoded before opening the file, takes an advisory exclusive
    lock where supported, writes one line with append semantics, and calls
    `fsync` before returning. Nothing holds the file open across beats — a
    session that survives a crashed listener is worth more than a buffered
    write.
    """

    def __init__(self, path):
        raw = os.path.abspath(os.path.expanduser(str(path)))
        # Canonicalise directory aliases (notably /tmp -> /private/tmp on
        # macOS) but deliberately do not resolve the final component: an
        # existing ledger symlink must be refused, not followed.
        self.path = os.path.join(os.path.realpath(os.path.dirname(raw)),
                                 os.path.basename(raw))

    def _assert_regular_not_symlink(self):
        try:
            mode = os.lstat(self.path).st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise SchemaError(f"ledger path is a symlink; refusing {self.path}")
        if not stat.S_ISREG(mode):
            raise SchemaError(f"ledger path is not a regular file: {self.path}")

    # ---- writing ----------------------------------------------------
    def append(self, etype, **fields):
        rec = make(etype, **fields)
        try:
            payload = (json.dumps(rec, ensure_ascii=False, allow_nan=False,
                                  separators=(",", ":")) + "\n").encode()
        except (TypeError, ValueError) as e:  # defensive; validate owns errors
            raise SchemaError(f"event is not JSON serializable: {e}") from e
        if len(payload) > MAX_LINE_BYTES:
            raise SchemaError(
                f"event exceeds the {MAX_LINE_BYTES}-byte ledger row limit")
        d = os.path.dirname(self.path)
        if d:
            _makedirs_private(d)
        self._assert_regular_not_symlink()
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as e:
            raise SchemaError(f"cannot open ledger safely at {self.path}: {e}") from e
        locked = False
        try:
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode):
                raise SchemaError(f"ledger path is not a regular file: {self.path}")
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
                locked = True
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("ledger write made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return rec

    # ---- reading ----------------------------------------------------
    def read(self, lane=None, etype=None):
        """All records, optionally filtered. Malformed lines are surfaced as
        `_malformed` records rather than dropped — a silently skipped line is
        the same failure mode as an unknown type."""
        out = []
        if not os.path.lexists(self.path):
            return out
        self._assert_regular_not_symlink()

        def malformed(line_no, error):
            # Typed/lane reads are state inputs. Diagnostics belong only in an
            # unfiltered audit read and must never affect counts/denominators.
            if lane is None and etype is None:
                out.append({"ts": 0.0, "type": "_malformed",
                            "line": line_no, "error": error})

        with open(self.path, "rb") as f:
            n = 0
            while True:
                raw = f.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                n += 1
                if len(raw) > MAX_LINE_BYTES:
                    if not raw.endswith(b"\n"):
                        while raw and not raw.endswith(b"\n"):
                            raw = f.readline(MAX_LINE_BYTES + 1)
                    malformed(n, f"row exceeds {MAX_LINE_BYTES} bytes")
                    continue
                try:
                    line = raw.decode("utf-8").strip()
                except UnicodeDecodeError as e:
                    malformed(n, f"invalid UTF-8: {e}")
                    continue
                if not line:
                    malformed(n, "blank ledger row is not valid JSON")
                    continue
                try:
                    rec = json.loads(line, parse_constant=_invalid_json_constant)
                    validate(rec)
                except (ValueError, SchemaError) as e:
                    malformed(n, str(e))
                    continue
                if lane and lane_of(rec.get("type", "")) != lane:
                    continue
                if etype and rec.get("type") != etype:
                    continue
                out.append(rec)
        return out

    def beats(self):
        return self.read(etype="ux.beat")

    def records(self):
        """Schema-valid records only, for state and metric consumers.

        ``read()`` without filters is the audit surface and includes
        ``_malformed`` diagnostics. Code that computes state, coverage, or a
        denominator must use this method (or a typed/lane read) instead.
        """
        return [rec for rec in self.read() if rec.get("type") != "_malformed"]

    def current_beat(self):
        """Index of the most recent GM beat, or 0 before the first one.

        UXR markers anchor to this so friction points at a cause instead of a
        clock reading.
        """
        return len(self.beats())

    def last(self, etype=None, lane=None):
        rows = self.read(lane=lane, etype=etype)
        return rows[-1] if rows else None


def write_atomic(path, text):
    """Used for derived artifacts (reports), never for the ledger itself."""
    d = os.path.dirname(path) or "."
    _makedirs_private(d)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _makedirs_private(path):
    """Create only missing directory components, each private by default."""
    path = os.path.abspath(path)
    missing = []
    cursor = path
    while not os.path.exists(cursor):
        missing.append(cursor)
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        cursor = parent
    if os.path.exists(cursor) and not os.path.isdir(cursor):
        raise SchemaError(f"ledger parent is not a directory: {cursor}")
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            if not os.path.isdir(directory):
                raise SchemaError(
                    f"ledger parent is not a directory: {directory}")
