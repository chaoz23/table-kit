"""Safe outbound Discord posting with a durable prepare/deliver/commit saga.

Posting is disabled unless ``transport.write_enabled`` is explicitly true.
Every enabled operation is prepared in the ledger before the first network
call, every returned Discord message ID is fsync'd as a receipt, and commit is
the final record.  Reusing an operation ID skips receipted chunks and requires
covered remote-history reconciliation before an unreceipted chunk can be sent.
One bounded, crash-released byte-range lock serializes that operation across
threads and local processes without blocking unrelated operation IDs.

Discord mentions are deny-by-default.  Generated/user text may contain
``@everyone``, roles, or arbitrary users, but only the explicitly cued agent's
configured user ID is present in ``allowed_mentions`` and that target appears
in content at most once.  Each chunk carries a deterministic nonce with
``enforce_nonce=true`` so an ambiguous timeout can be retried safely while
Discord retains nonce uniqueness.

The transport is still an example, not a control plane.  This module provides
deterministic seams for tests and adapters; production enablement remains
gated on authenticated ingestion, durable source continuity, and live chaos
testing documented in ``docs/TRANSPORT.md``.
"""

import errno
import hashlib
import json
import math
import os
import random
import re
import stat
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import weakref
from contextlib import contextmanager
from datetime import datetime

from .config import ConfigError
from .events import SchemaError, _makedirs_private

try:  # POSIX byte-range locks provide crash-released cross-process ownership.
    import fcntl
except ImportError:  # pragma: no cover - live posting fails closed on Windows
    fcntl = None


DISCORD_CONTENT_LIMIT = 2000
CHUNK_LIMIT = DISCORD_CONTENT_LIMIT  # compatibility name
NONCE_LIMIT = 25
MAX_CHUNKS_PER_OPERATION = 10
MAX_SOURCE_UTF16_UNITS = DISCORD_CONTENT_LIMIT * MAX_CHUNKS_PER_OPERATION
MAX_ATTEMPTS = 3
MAX_RETRY_BUDGET_S = 120.0
MAX_HISTORY_PAGES = 10
HISTORY_PAGE_SIZE = 100
SAGA_LOCK_TIMEOUT_S = 5.0
SAGA_LOCK_POLL_S = 0.05

MENTION_RE = re.compile(r"<@!?(\d+)>")
SNOWFLAKE_RE = re.compile(r"^[1-9]\d{0,19}$")
MAX_SNOWFLAKE = 2 ** 64 - 1
DISCORD_EPOCH_MS = 1420070400000
OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class _LocalSagaLock:
    """Weak-referenceable wrapper around the non-weakrefable lock on 3.9."""

    def __init__(self):
        self.lock = threading.Lock()


_LOCAL_SAGA_LOCKS = weakref.WeakValueDictionary()
_LOCAL_SAGA_LOCKS_GUARD = threading.Lock()


class PostError(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class DeliveryError(PostError):
    def __init__(self, code, detail, retryable=False, retry_after=None,
                 delivery_uncertain=False):
        super().__init__(code, detail)
        self.retryable = retryable
        self.retry_after = retry_after
        self.delivery_uncertain = delivery_uncertain


def _local_saga_lock(key):
    with _LOCAL_SAGA_LOCKS_GUARD:
        holder = _LOCAL_SAGA_LOCKS.get(key)
        if holder is None:
            holder = _LocalSagaLock()
            _LOCAL_SAGA_LOCKS[key] = holder
        return holder


def _lock_file(ledger, operation_id):
    """Return a private lock file and byte offset for one ledger operation."""
    ledger_path = getattr(ledger, "path", None)
    if not isinstance(ledger_path, str) or not os.path.isabs(ledger_path):
        raise PostError(
            "invalid_ledger",
            "posting requires a Ledger with a canonical absolute path")
    canonical = os.path.join(os.path.realpath(os.path.dirname(ledger_path)),
                             os.path.basename(ledger_path))
    if canonical != ledger_path:
        raise PostError("invalid_ledger", "ledger path is not canonical")
    # Preserve Ledger's refusal to follow a final-component symlink before a
    # sibling lock is allowed to confer authority to write.
    checker = getattr(ledger, "_assert_regular_not_symlink", None)
    if not callable(checker):
        raise PostError("invalid_ledger", "posting requires a safe Ledger path")
    try:
        checker()
        _makedirs_private(os.path.dirname(ledger_path))
    except (OSError, SchemaError) as error:
        raise PostError("invalid_ledger", str(error)) from error

    lock_path = ledger_path + ".post.lock"
    try:
        before = os.lstat(lock_path)
    except FileNotFoundError:
        before = None
    except OSError as error:
        raise PostError("saga_lock_failed",
                        f"cannot inspect posting lock safely: {error}") from error
    if before is not None and (stat.S_ISLNK(before.st_mode)
                               or not stat.S_ISREG(before.st_mode)):
        raise PostError("unsafe_saga_lock",
                        f"posting lock is not a regular file: {lock_path}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise PostError("saga_lock_failed",
                        f"cannot open posting lock safely: {error}") from error
    try:
        opened = os.fstat(fd)
        after = os.lstat(lock_path)
        if (not stat.S_ISREG(opened.st_mode)
                or stat.S_ISLNK(after.st_mode)
                or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)):
            raise PostError("unsafe_saga_lock",
                            "posting lock changed identity while it was opened")
        if stat.S_IMODE(opened.st_mode) & 0o077:
            raise PostError(
                "unsafe_saga_lock",
                "posting lock must not be accessible by group or other users")
    except OSError as error:
        os.close(fd)
        raise PostError("saga_lock_failed",
                        f"cannot validate posting lock safely: {error}") from error
    except BaseException:
        os.close(fd)
        raise
    # One file avoids an unbounded directory of operation lock files.  POSIX
    # byte-range locks let unrelated operations progress independently; the
    # local lock below supplies the equivalent exclusion between threads,
    # because fcntl record locks alone are process-scoped.
    digest = hashlib.sha256(operation_id.encode("ascii")).digest()
    offset = int.from_bytes(digest[:8], "big") % (2 ** 62) + 1
    return fd, lock_path, offset


@contextmanager
def _saga_lock(ledger, operation_id, timeout_s=SAGA_LOCK_TIMEOUT_S):
    """Bounded, crash-released ownership of one outbound operation."""
    if fcntl is None:
        raise PostError(
            "saga_lock_unavailable",
            "safe outbound posting requires POSIX advisory byte-range locks")
    if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
            or not 0 <= timeout_s < float("inf")):
        raise PostError("invalid_lock_policy",
                        "saga lock timeout must be finite and non-negative")
    fd, lock_path, offset = _lock_file(ledger, operation_id)
    holder = _local_saga_lock((lock_path, offset))
    deadline = time.monotonic() + float(timeout_s)
    remaining = max(0.0, deadline - time.monotonic())
    acquired_local = holder.lock.acquire(timeout=remaining)
    if not acquired_local:
        os.close(fd)
        raise PostError("operation_busy",
                        f"operation {operation_id!r} is already being delivered")
    acquired_file = False
    try:
        while True:
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB,
                            1, offset, os.SEEK_SET)
                acquired_file = True
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise PostError("saga_lock_failed",
                                    f"cannot acquire posting lock: {error}") from error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PostError(
                        "operation_busy",
                        f"operation {operation_id!r} is already being delivered")
                time.sleep(min(SAGA_LOCK_POLL_S, remaining))
        yield
    finally:
        if acquired_file:
            fcntl.lockf(fd, fcntl.LOCK_UN, 1, offset, os.SEEK_SET)
        os.close(fd)
        holder.lock.release()


def utf16_units(text):
    """Discord-compatible conservative content measure.

    Discord documents a 2,000-character content limit.  Counting UTF-16 code
    units is conservative for astral characters and agrees with Discord's
    JavaScript-facing representation; no chunk can exceed the documented cap.
    """
    try:
        return len(text.encode("utf-16-le")) // 2
    except UnicodeEncodeError as error:
        raise PostError("invalid_unicode",
                        "message content contains an unpaired Unicode surrogate") from error


def _is_extend(char):
    cp = ord(char)
    return (unicodedata.category(char) in {"Mn", "Mc", "Me"}
            or cp in {0x0E33, 0x0EB3, 0x200C}
            or 0xFF9E <= cp <= 0xFF9F
            or 0xFE00 <= cp <= 0xFE0F
            or 0xE0100 <= cp <= 0xE01EF
            or 0x1F3FB <= cp <= 0x1F3FF
            or 0xE0020 <= cp <= 0xE007F)


def _is_regional(char):
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


def _hangul_type(char):
    cp = ord(char)
    if 0x1100 <= cp <= 0x115F or 0xA960 <= cp <= 0xA97C:
        return "L"
    if 0x1160 <= cp <= 0x11A7 or 0xD7B0 <= cp <= 0xD7C6:
        return "V"
    if 0x11A8 <= cp <= 0x11FF or 0xD7CB <= cp <= 0xD7FB:
        return "T"
    if 0xAC00 <= cp <= 0xD7A3:
        return "LV" if (cp - 0xAC00) % 28 == 0 else "LVT"
    return None


def _is_prepend(char):
    """Unicode 17 Grapheme_Cluster_Break=Prepend code points."""
    cp = ord(char)
    return (0x0600 <= cp <= 0x0605
            or cp in {0x06DD, 0x070F, 0x08E2, 0x0D4E, 0x110BD, 0x110CD,
                      0x113D1, 0x1193F, 0x11941, 0x11D46, 0x11F02}
            or 0x0890 <= cp <= 0x0891
            or 0x111C2 <= cp <= 0x111C3
            or 0x11A84 <= cp <= 0x11A89)


def _is_conjunct_linker(char):
    name = unicodedata.name(char, "")
    return any(token in name for token in
               ("VIRAMA", "HALANT", "COENG", "KILLER", "PULLI", "ASAT"))


def _ends_conjunct_linker(cluster):
    for char in reversed(cluster):
        if _is_conjunct_linker(char):
            return True
        if char == "\u200d" or _is_extend(char):
            continue
        return False
    return False


def _graphemes(text):
    """A conservative stdlib grapheme iterator for transport boundaries.

    It keeps combining marks, variation selectors, emoji modifiers, keycaps,
    ZWJ sequences, CRLF, tag sequences, and regional-indicator flag pairs
    together.  A pathological cluster larger than the platform cap is refused
    instead of split into invalid Unicode presentation fragments.
    """
    cluster = ""
    regional_count = 0
    for char in text:
        if not cluster:
            cluster = char
            regional_count = 1 if _is_regional(char) else 0
            continue
        previous_hangul = _hangul_type(cluster[-1])
        current_hangul = _hangul_type(char)
        hangul_join = (
            previous_hangul == "L"
            and current_hangul in {"L", "V", "LV", "LVT"}
        ) or (
            previous_hangul in {"LV", "V"}
            and current_hangul in {"V", "T"}
        ) or (
            previous_hangul in {"LVT", "T"} and current_hangul == "T"
        )
        join = (_is_extend(char) or char == "\u200d"
                or cluster.endswith("\u200d")
                or _is_prepend(cluster[-1])
                or hangul_join
                or (_ends_conjunct_linker(cluster)
                    and unicodedata.category(char).startswith(("L", "M")))
                or (cluster == "\r" and char == "\n")
                or (_is_regional(char) and regional_count == 1))
        if join:
            cluster += char
            if _is_regional(char):
                regional_count += 1
            continue
        yield cluster
        cluster = char
        regional_count = 1 if _is_regional(char) else 0
    if cluster:
        yield cluster


def _prefix_within(text, limit):
    units = 0
    chars = 0
    for cluster in _graphemes(text):
        size = utf16_units(cluster)
        if size > limit and not chars:
            raise PostError(
                "grapheme_too_large",
                f"one Unicode grapheme requires {size} UTF-16 units; Discord's "
                f"content limit is {limit}")
        if units + size > limit:
            break
        units += size
        chars += len(cluster)
    return chars


def split(text, limit=CHUNK_LIMIT):
    """Split without exceeding ``limit`` UTF-16 units or breaking graphemes.

    Paragraph, line, and whitespace boundaries are preferred.  An unbroken
    token longer than the platform limit must be hard-split, but only between
    grapheme clusters.
    """
    if not isinstance(text, str):
        raise PostError("invalid_content", "message content must be a string")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise PostError("invalid_limit", "chunk limit must be a positive integer")
    if not text:
        return []
    chunks = []
    remaining = text
    while utf16_units(remaining) > limit:
        end = _prefix_within(remaining, limit)
        candidate = remaining[:end]
        boundaries = []
        for separator in ("\n\n", "\n", " ", "\t"):
            at = candidate.rfind(separator)
            if at > 0:
                boundaries.append(at)
        cut = max(boundaries) if boundaries else end
        chunk = remaining[:cut].rstrip()
        if not chunk:
            cut = end
            chunk = remaining[:cut]
        if utf16_units(chunk) > limit:
            raise AssertionError("splitter produced an oversized chunk")
        chunks.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    if any(utf16_units(chunk) > limit for chunk in chunks):
        raise AssertionError("splitter produced an oversized chunk")
    return chunks


def _mention_user_id(seat):
    if not seat or seat.kind != "agent":
        return None
    match = MENTION_RE.fullmatch(seat.mention or "")
    if not match or not _is_discord_snowflake(match.group(1)):
        raise PostError(
            "invalid_target_mention",
            f"agent seat {seat.id!r} must use a Discord user mention like <@123>")
    return match.group(1)


def _is_discord_snowflake(value):
    return (isinstance(value, str) and SNOWFLAKE_RE.fullmatch(value) is not None
            and int(value) <= MAX_SNOWFLAKE)


def _snowflake_timestamp(value):
    return ((int(value) >> 22) + DISCORD_EPOCH_MS) / 1000.0


def _discord_id(value, field):
    if not _is_discord_snowflake(value):
        raise PostError(
            f"invalid_{field}",
            f"transport.{field} must be a canonical unsigned 64-bit Discord snowflake")
    return value


def _strip_target_mentions(text, user_id):
    count = 0

    def replace(match):
        nonlocal count
        if (match.group(1).lstrip("0") or "0") == user_id:
            count += 1
            return ""
        return match.group(0)

    return MENTION_RE.sub(replace, text), count


def ensure_mention(cfg, text, seat):
    """Return ``(text, repaired)`` with exactly one configured target mention."""
    del cfg  # retained in the public signature for compatibility
    user_id = _mention_user_id(seat)
    if user_id is None:
        return text, False
    base, count = _strip_target_mentions(text, user_id)
    if count == 1:
        return text, False
    base = base.rstrip()
    repaired = f"{base} {seat.mention}" if base else seat.mention
    return repaired, True


def split_with_mention(text, seat, limit=CHUNK_LIMIT):
    """Split and put the sole target mention in the first bounded chunk."""
    user_id = _mention_user_id(seat)
    if user_id is None:
        return split(text, limit=limit)
    base, _ = _strip_target_mentions(text, user_id)
    base = base.strip()
    suffix = (" " if base else "") + seat.mention
    budget = limit - utf16_units(suffix)
    if budget < 1 and base:
        raise PostError("target_mention_too_large",
                        "configured target mention leaves no content capacity")
    chunks = split(base, limit=budget) if base else [""]
    chunks[0] = (chunks[0].rstrip() + suffix).strip()
    if any(utf16_units(chunk) > limit for chunk in chunks):
        raise AssertionError("mention-aware splitter produced an oversized chunk")
    if sum(chunk.count(seat.mention) for chunk in chunks) != 1:
        raise AssertionError("target mention must appear exactly once")
    return chunks


def _nonce(operation_id, index, transport=None):
    # Discord checks nonce uniqueness by author rather than by destination.
    # Scope an explicit operation ID to its author/channel so two tables that
    # choose the same human-readable ID cannot suppress each other's messages.
    identity = transport or {}
    raw = json.dumps({
        "operation_id": operation_id,
        "chunk_index": index,
        "bot_user_id": identity.get("bot_user_id"),
        "channel_id": identity.get("channel_id"),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:NONCE_LIMIT]


def _allowed_mentions(user_id):
    if user_id is None:
        return {"parse": [], "replied_user": False}
    # Discord rejects combining `parse` with explicit users.  Supplying only
    # the user allowlist means roles/everyone/other users remain unparsed.
    return {"users": [user_id], "replied_user": False}


def _operation_id(value=None):
    value = value or f"post-{uuid.uuid4().hex}"
    if not isinstance(value, str) or not OPERATION_RE.fullmatch(value):
        raise PostError(
            "invalid_operation_id",
            "operation_id must be 1-128 ASCII letters, digits, '.', '_', ':' or '-', "
            "starting with a letter or digit")
    return value


def _transport_identity(cfg):
    return {
        "kind": cfg.transport.get("kind"),
        "channel_id": cfg.transport.get("channel_id"),
        "bot_user_id": cfg.transport.get("bot_user_id"),
        "token_env": cfg.transport.get("token_env", "TABLE_BOT_TOKEN"),
    }


def _validate_live_config(cfg, require_token=False):
    if not cfg.transport.get("channel_id"):
        raise PostError("missing_channel", "transport.channel_id is required")
    if not cfg.transport.get("bot_user_id"):
        raise PostError(
            "missing_bot_user",
            "transport.bot_user_id is required for authenticated crash recovery")
    _discord_id(cfg.transport["channel_id"], "channel_id")
    _discord_id(cfg.transport["bot_user_id"], "bot_user_id")
    if require_token:
        cfg.token()


def prepare(cfg, text, cue=None, kind=None, operation_id=None):
    """Validate and produce an immutable outbound plan without I/O."""
    operation_id = _operation_id(operation_id)
    if not isinstance(text, str) or not text.strip():
        raise PostError("invalid_content", "message content must be non-empty text")
    source_units = utf16_units(text)
    if source_units > MAX_SOURCE_UTF16_UNITS:
        raise PostError(
            "content_too_large",
            f"one operation accepts at most {MAX_SOURCE_UTF16_UNITS} UTF-16 "
            f"units before mention repair; this input has {source_units}")
    if kind is not None and (not isinstance(kind, str) or not kind.strip()):
        raise PostError("invalid_kind", "message kind must be non-empty text")
    seat = None
    if cue is not None:
        if not isinstance(cue, str) or not cue.strip():
            raise PostError("invalid_target", "cue target must be a non-empty seat ID")
        seat = cfg.seat(cue)
        if seat is None:
            raise PostError("unknown_target", f"unknown cue target {cue!r}")
    user_id = _mention_user_id(seat)
    outbound, repaired = ensure_mention(cfg, text, seat)
    chunks = split_with_mention(outbound, seat, limit=DISCORD_CONTENT_LIMIT)
    if not chunks:
        raise PostError("invalid_content", "message content produced no chunks")
    if len(chunks) > MAX_CHUNKS_PER_OPERATION:
        raise PostError(
            "too_many_chunks",
            f"one operation may send at most {MAX_CHUNKS_PER_OPERATION} messages; "
            f"this beat requires {len(chunks)}")
    transport = _transport_identity(cfg)
    payloads = []
    for index, chunk in enumerate(chunks):
        payloads.append({
            "content": chunk,
            "allowed_mentions": _allowed_mentions(user_id if index == 0 else None),
            "nonce": _nonce(operation_id, index, transport),
            "enforce_nonce": True,
        })
    canonical = json.dumps({
        "cue": seat.id if seat else None,
        "kind": kind,
        "payloads": payloads,
        "transport": transport,
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    plan_digest = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    content_digest = "sha256:" + hashlib.sha256(
        outbound.encode("utf-8")).hexdigest()
    pair_id = ("cue-" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:24]
               if seat else None)
    return {
        "operation_id": operation_id,
        "seat": seat,
        "kind": kind,
        "source_text": text,
        "text": outbound,
        "chunks": chunks,
        "payloads": payloads,
        "transport": transport,
        "mention_repaired": repaired,
        "plan_digest": plan_digest,
        "content_digest": content_digest,
        "pair_id": pair_id,
    }


def discord_send(cfg, payload):
    """POST one fully specified Discord message and return its message object."""
    channel_id = _discord_id(cfg.transport.get("channel_id"), "channel_id")
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bot {cfg.token()}",
                 "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://github.com/chaoz23/table-kit, 0.5)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.load(response)
    except ValueError as error:
        raise DeliveryError(
            "invalid_response", "Discord returned malformed JSON",
            retryable=False, delivery_uncertain=True) from error
    if not isinstance(result, dict):
        raise DeliveryError("invalid_response", "Discord returned a non-object response",
                            retryable=False, delivery_uncertain=True)
    message_id = result.get("id")
    if not _is_discord_snowflake(message_id):
        raise DeliveryError(
            "invalid_response", "Discord response omitted a valid message snowflake",
            retryable=False, delivery_uncertain=True)
    if result.get("nonce") is None or str(result.get("nonce")) != payload["nonce"]:
        raise DeliveryError(
            "nonce_mismatch", "Discord response did not echo the requested nonce",
            retryable=False, delivery_uncertain=True)
    if result.get("content") != payload["content"]:
        raise DeliveryError(
            "content_mismatch", "Discord response did not contain the requested content",
            retryable=False, delivery_uncertain=True)
    author = result.get("author")
    bot_user_id = cfg.transport.get("bot_user_id")
    if (not isinstance(author, dict)
            or str(author.get("id")) != str(bot_user_id)):
        raise DeliveryError(
            "author_mismatch",
            "Discord response author did not match transport.bot_user_id",
            retryable=False, delivery_uncertain=True)
    if str(result.get("channel_id")) != channel_id:
        raise DeliveryError(
            "channel_mismatch",
            "Discord response channel did not match transport.channel_id",
            retryable=False, delivery_uncertain=True)
    return result


def _parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def discord_history_since(cfg, since_ts, max_pages=MAX_HISTORY_PAGES):
    """Fetch bounded channel history back through ``since_ts`` for recovery."""
    if (isinstance(since_ts, bool)
            or not isinstance(since_ts, (int, float))
            or not math.isfinite(since_ts) or since_ts < 0):
        raise PostError("invalid_history_request",
                        "history start must be a finite epoch timestamp")
    if (isinstance(max_pages, bool) or not isinstance(max_pages, int)
            or max_pages < 1 or max_pages > MAX_HISTORY_PAGES):
        raise PostError(
            "invalid_history_request",
            f"history pages must be between 1 and {MAX_HISTORY_PAGES}")
    channel_id = _discord_id(cfg.transport.get("channel_id"), "channel_id")
    before = None
    messages = []
    complete = False
    previous_id = None
    previous_timestamp = None
    seen_ids = set()
    for _ in range(max_pages):
        query = {"limit": HISTORY_PAGE_SIZE}
        if before:
            query["before"] = before
        url = (f"https://discord.com/api/v10/channels/{channel_id}/messages?"
               + urllib.parse.urlencode(query))
        req = urllib.request.Request(
            url, method="GET",
            headers={"Authorization": f"Bot {cfg.token()}",
                     "User-Agent": "DiscordBot (https://github.com/chaoz23/table-kit, 0.5)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                page = json.load(response)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise PostError(
                "history_http_error",
                f"Discord history request failed with HTTP {status}") from error
        except ValueError as error:
            raise PostError("invalid_history",
                            "Discord history returned malformed JSON") from error
        if not isinstance(page, list):
            raise PostError("invalid_history", "Discord history was not a list")
        if len(page) > HISTORY_PAGE_SIZE:
            raise PostError("invalid_history", "Discord history page exceeded its limit")
        if not page:
            complete = True
            break
        timestamps = []
        for message in page:
            if not isinstance(message, dict):
                raise PostError("invalid_history",
                                "Discord history contained a non-message value")
            message_id = message.get("id")
            if not _is_discord_snowflake(message_id):
                raise PostError("invalid_history",
                                "Discord history message has an invalid snowflake ID")
            if message_id in seen_ids:
                raise PostError("invalid_history",
                                "Discord history pagination repeated a message ID")
            timestamp = _parse_timestamp(message.get("timestamp"))
            if timestamp is None:
                raise PostError("invalid_history",
                                "Discord history message has an invalid timestamp")
            if abs(timestamp - _snowflake_timestamp(message_id)) > 1.0:
                raise PostError(
                    "invalid_history",
                    "Discord history timestamp disagrees with its snowflake ID")
            numeric_id = int(message_id)
            if previous_id is not None and numeric_id >= previous_id:
                raise PostError("invalid_history",
                                "Discord history was not newest-to-oldest")
            if (previous_timestamp is not None
                    and timestamp > previous_timestamp):
                raise PostError("invalid_history",
                                "Discord history timestamps were not newest-to-oldest")
            previous_id = numeric_id
            previous_timestamp = timestamp
            seen_ids.add(message_id)
            timestamps.append(timestamp)
        messages.extend(page)
        if timestamps[-1] <= since_ts:
            complete = True
            break
        before = page[-1]["id"]
    return {"complete": complete, "messages": messages}


def _http_error(error):
    status = getattr(error, "code", None)
    retry_after = None
    if status == 429:
        header = None
        try:
            header = error.headers.get("Retry-After") if error.headers else None
        except (AttributeError, TypeError):
            header = None
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, ValueError):
            payload = {}
        candidate = payload.get("retry_after") if isinstance(payload, dict) else None
        candidate = candidate if isinstance(candidate, (int, float)) else header
        try:
            retry_after = float(candidate)
        except (TypeError, ValueError):
            retry_after = None
        return DeliveryError(
            "rate_limited", "Discord rate limited the message",
            retryable=retry_after is not None and retry_after >= 0,
            retry_after=retry_after, delivery_uncertain=False)
    if isinstance(status, int) and 500 <= status <= 599:
        return DeliveryError("discord_server_error", f"Discord HTTP {status}",
                             retryable=True, delivery_uncertain=True)
    return DeliveryError("discord_http_error", f"Discord HTTP {status or 'error'}",
                         retryable=False, delivery_uncertain=False)


def _delivery_error(error):
    if isinstance(error, DeliveryError):
        return error
    if isinstance(error, PostError):
        return DeliveryError(error.code, error.detail, retryable=False,
                             delivery_uncertain=False)
    if isinstance(error, ConfigError):
        return DeliveryError("invalid_config", str(error)[:300], retryable=False,
                             delivery_uncertain=False)
    if isinstance(error, urllib.error.HTTPError):
        # ``HTTPError`` is also the response body.  Close it on every status,
        # including 4xx/5xx paths whose body we do not otherwise consume, so a
        # failed post cannot leak one socket/file descriptor per attempt.
        try:
            return _http_error(error)
        finally:
            error.close()
    if isinstance(error, (urllib.error.URLError, TimeoutError, OSError)):
        return DeliveryError("transport_error", str(error)[:300], retryable=True,
                             delivery_uncertain=True)
    if isinstance(error, ValueError):
        return DeliveryError("invalid_response", str(error)[:300], retryable=True,
                             delivery_uncertain=True)
    return DeliveryError("delivery_failed", str(error)[:300], retryable=False,
                         delivery_uncertain=True)


def _normalize_send_result(result, payload):
    if not isinstance(result, dict):
        raise DeliveryError(
            "invalid_response", "sender must return a message object with id and nonce",
            retryable=False, delivery_uncertain=True)
    message_id = result.get("id")
    echoed_nonce = result.get("nonce")
    if not isinstance(message_id, str) or not message_id:
        raise DeliveryError(
            "invalid_response", "sender response omitted a message ID",
            retryable=False, delivery_uncertain=True)
    if echoed_nonce is None or str(echoed_nonce) != payload["nonce"]:
        raise DeliveryError("nonce_mismatch", "sender did not echo the requested nonce",
                            retryable=False, delivery_uncertain=True)
    return message_id


def _send_with_retry(send, cfg, payload, sleep_fn, random_fn,
                     max_attempts=MAX_ATTEMPTS,
                     retry_budget_s=MAX_RETRY_BUDGET_S,
                     retry_state=None):
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) \
            or max_attempts < 1:
        raise DeliveryError("invalid_retry_policy",
                            "max_attempts must be a positive integer")
    if isinstance(retry_budget_s, bool) \
            or not isinstance(retry_budget_s, (int, float)) \
            or not 0 <= retry_budget_s < float("inf"):
        raise DeliveryError("invalid_retry_policy",
                            "retry_budget_s must be a finite non-negative number")
    retry_state = retry_state if retry_state is not None else {"waited": 0.0}
    waited = retry_state.get("waited", 0.0)
    if isinstance(waited, bool) or not isinstance(waited, (int, float)) \
            or not 0 <= waited < float("inf"):
        raise DeliveryError("invalid_retry_policy", "retry state is invalid")
    for attempt in range(max_attempts):
        try:
            return _normalize_send_result(send(cfg, payload), payload)
        except Exception as raw:  # transport seam; classified below
            error = _delivery_error(raw)
        if not error.retryable or attempt + 1 >= max_attempts:
            raise error
        base = error.retry_after if error.retry_after is not None \
            else min(2 ** attempt, 8)
        jitter = min(max(float(random_fn()), 0.0), 1.0) * 0.25
        delay = float(base) + jitter
        if delay < 0 or waited + delay > retry_budget_s:
            raise DeliveryError(
                "retry_budget_exceeded",
                f"retry requires {delay:.3f}s beyond the {retry_budget_s:.3f}s budget",
                retryable=False,
                delivery_uncertain=error.delivery_uncertain)
        sleep_fn(delay)
        waited += delay
        retry_state["waited"] = waited
    raise AssertionError("retry loop exhausted without returning or raising")


def _operation_rows(ledger, operation_id):
    return [row for row in ledger.records()
            if row.get("operation_id") == operation_id]


def _audit_ledger(ledger):
    """Refuse outbound writes when any existing ledger row is unreadable.

    ``Ledger.records()`` intentionally omits diagnostic ``_malformed`` rows for
    reporting consumers. A posting saga cannot do that safely: an omitted row
    might be the prepare or receipt that proves a remote send already happened.
    """
    try:
        rows = ledger.read()
    except (OSError, SchemaError) as error:
        raise PostError("invalid_ledger", str(error)) from error
    malformed = [row for row in rows if row.get("type") == "_malformed"]
    if malformed:
        first = malformed[0]
        raise PostError(
            "malformed_ledger",
            "outbound posting requires an entirely readable ledger; "
            f"line {first.get('line', '?')} is malformed")


def _rows_of(rows, etype):
    return [row for row in rows if row.get("type") == etype]


def _receipt_map(rows):
    receipts = {}
    for row in _rows_of(rows, "qa.post.receipt"):
        index = row.get("chunk_index")
        previous = receipts.get(index)
        if previous and (previous.get("message_id") != row.get("message_id")
                         or previous.get("nonce") != row.get("nonce")):
            raise PostError("conflicting_receipts",
                            f"chunk {index} has conflicting durable receipts")
        receipts[index] = row
    return receipts


def _validate_receipts(plan, receipts):
    message_ids = {}
    for index, row in receipts.items():
        if isinstance(index, bool) or not isinstance(index, int) \
                or index < 0 or index >= len(plan["payloads"]):
            raise PostError("invalid_receipt",
                            f"receipt chunk index {index!r} is outside the plan")
        if row.get("nonce") != plan["payloads"][index]["nonce"]:
            raise PostError("invalid_receipt",
                            f"receipt nonce does not match chunk {index}")
        if not isinstance(row.get("message_id"), str) or not row["message_id"]:
            raise PostError("invalid_receipt", f"receipt {index} has no message ID")
        other = message_ids.get(row["message_id"])
        if other is not None and other != index:
            raise PostError(
                "invalid_receipt",
                f"message ID {row['message_id']!r} is associated with chunks "
                f"{other} and {index}")
        message_ids[row["message_id"]] = index
    return receipts


def _append_once(ledger, etype, operation_id, **fields):
    rows = _operation_rows(ledger, operation_id)
    existing = [row for row in rows if row.get("type") == etype]
    if len(existing) > 1:
        raise PostError("invalid_saga",
                        f"operation has duplicate {etype} records")
    if existing:
        # Latency and reconciliation counts can legitimately differ after a
        # crash.  Every semantic field must still match the immutable plan.
        mismatch = _semantic_mismatch(existing[0], fields)
        if mismatch:
            raise PostError(
                "invalid_saga",
                f"existing {etype} disagrees on {', '.join(sorted(mismatch))}")
        return None
    record, created = ledger.append_once(
        etype, {"operation_id": operation_id},
        operation_id=operation_id, **fields)
    if not created:
        mismatch = _semantic_mismatch(record, fields)
        if mismatch:
            raise PostError(
                "invalid_saga",
                f"existing {etype} disagrees on {', '.join(sorted(mismatch))}")
        return None
    return record


def _append_receipt_once(ledger, operation_id, chunk_index, message_id,
                         nonce, reconciled):
    """Give each planned chunk one atomic, immutable durable receipt."""
    fields = {
        "operation_id": operation_id,
        "chunk_index": chunk_index,
        "message_id": message_id,
        "nonce": nonce,
        "reconciled": reconciled,
    }
    record, created = ledger.append_once(
        "qa.post.receipt",
        {"operation_id": operation_id, "chunk_index": chunk_index},
        **fields)
    if not created:
        mismatch = _semantic_mismatch(record, fields)
        if mismatch:
            raise PostError(
                "conflicting_receipts",
                f"chunk {chunk_index} already has a different durable receipt")
    return record


def _semantic_mismatch(row, fields):
    volatile = {"latency_ms", "remote_matches", "history_complete", "beat"}
    return [key for key, value in fields.items()
            if key not in volatile and row.get(key) != value]


def _require_final_record(rows, etype, fields):
    existing = _rows_of(rows, etype)
    if len(existing) != 1:
        raise PostError(
            "invalid_saga",
            f"committed operation requires exactly one {etype} record")
    mismatch = _semantic_mismatch(existing[0], fields)
    if mismatch:
        raise PostError(
            "invalid_saga",
            f"existing {etype} disagrees on {', '.join(sorted(mismatch))}")


def _record_failure(ledger, plan, error, chunks_sent):
    operation_id = plan["operation_id"]
    planned = len(plan["chunks"])
    fields = {
        "operation_id": operation_id,
        "error": f"{error.code}: {error.detail}"[:500],
        "error_code": error.code,
        "chunks_sent": chunks_sent,
        "chunks_planned": planned,
        "delivery_uncertain": error.delivery_uncertain,
    }
    rows = _operation_rows(ledger, operation_id)
    state_key = (error.code, chunks_sent)
    seen = {(row.get("error_code"), row.get("chunks_sent"))
            for row in _rows_of(rows, "qa.post_failed")}
    if state_key not in seen:
        ledger.append("qa.post_failed", **fields)
    if chunks_sent and not any(
            row.get("type") == "qa.post.partial"
            and row.get("chunks_sent") == chunks_sent for row in rows):
        ledger.append("qa.post.partial", operation_id=operation_id,
                      chunks_sent=chunks_sent, chunks_planned=planned,
                      error=fields["error"], error_code=error.code,
                      delivery_uncertain=error.delivery_uncertain)


def _reconcile(cfg, ledger, plan, history_fn):
    operation_id = plan["operation_id"]
    rows = _operation_rows(ledger, operation_id)
    receipts = _validate_receipts(plan, _receipt_map(rows))
    missing = [i for i in range(len(plan["payloads"])) if i not in receipts]
    if not missing:
        return receipts
    prepared = _rows_of(rows, "qa.post.prepare")
    if len(prepared) != 1:
        raise PostError("invalid_saga", "resume requires exactly one prepare record")
    result = history_fn(cfg, prepared[0]["ts"])
    if not isinstance(result, dict) or not isinstance(result.get("messages"), list) \
            or not isinstance(result.get("complete"), bool):
        raise PostError("invalid_history",
                        "history_fn must return {complete: bool, messages: list}")
    bot_user_id = cfg.transport.get("bot_user_id")
    by_nonce = {}
    expected = {payload["nonce"]: (index, payload)
                for index, payload in enumerate(plan["payloads"])}
    for message in result["messages"]:
        if not isinstance(message, dict):
            raise PostError("invalid_history",
                            "history contained a non-message value")
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise PostError("invalid_history", "history message has no stable ID")
        author = message.get("author") or {}
        if bot_user_id:
            if not isinstance(author, dict) or not author.get("id"):
                raise PostError("invalid_history",
                                "history message has no authenticated author")
            if str(author.get("id")) != str(bot_user_id):
                continue
        nonce = str(message.get("nonce")) if message.get("nonce") is not None else None
        if nonce not in expected:
            continue
        index, payload = expected[nonce]
        if message.get("content") != payload["content"]:
            raise PostError("reconcile_content_mismatch",
                            f"remote nonce for chunk {index} has different content")
        by_nonce.setdefault(nonce, []).append(message_id)
    for nonce, ids in by_nonce.items():
        if len(set(ids)) != 1:
            raise PostError("ambiguous_history",
                            f"remote history has multiple messages for nonce {nonce}")
        index, _ = expected[nonce]
        if index not in receipts:
            _append_receipt_once(
                ledger, operation_id, index, ids[0], nonce, reconciled=True)
    receipts = _validate_receipts(
        plan, _receipt_map(_operation_rows(ledger, operation_id)))
    still_missing = [i for i in missing if i not in receipts]
    if still_missing and not result["complete"]:
        raise PostError(
            "reconciliation_incomplete",
            "remote history did not cover the prepare time; refusing a retry "
            f"for unreceipted chunk(s) {still_missing}")
    _append_once(ledger, "qa.post.reconcile", operation_id,
                 state="complete", remote_matches=len(by_nonce),
                 history_complete=result["complete"])
    return receipts


def _finalize(ledger, plan, latency_ms):
    operation_id = plan["operation_id"]
    rows = _operation_rows(ledger, operation_id)
    receipts = _validate_receipts(plan, _receipt_map(rows))
    if len(receipts) != len(plan["chunks"]):
        raise PostError("incomplete_delivery", "cannot commit without every receipt")
    commits = _rows_of(rows, "qa.post.commit")
    message_ids = [receipts[i]["message_id"] for i in range(len(receipts))]
    seat = plan["seat"]
    text = plan["text"]
    final_records = [
        ("ux.beat", {
            "words": len(text.split()), "chunks": len(plan["chunks"]),
            "kind": plan["kind"], "cued_seat": seat.id if seat else None,
            "mention_ok": True, "text": text[:400],
        }),
        ("event", {"text": text[:400], "actor": "GM"}),
    ]
    if plan["mention_repaired"]:
        final_records.append((
            "qa.command", {"cmd": "mention_repair", "ok": True,
                           "seat": seat.id}))
    if seat:
        final_records.append((
            "out.open", {"pair": "cue", "id": plan["pair_id"],
                         "seat": seat.id, "detail": text[:120]}))
    final_records.append((
        "qa.post", {"ok": True, "chars": len(text),
                    "chunks": len(plan["chunks"]), "latency_ms": latency_ms,
                    "mention_repaired": plan["mention_repaired"] or None,
                    "message_ids": message_ids}))
    if commits:
        if len(commits) != 1:
            raise PostError("invalid_saga", "operation has duplicate commit records")
        if (commits[0].get("chunks") != len(plan["chunks"])
                or commits[0].get("message_ids") != message_ids):
            raise PostError("invalid_saga",
                            "commit record disagrees with durable receipts")
        # Commit is a terminal assertion that all derived table state was
        # durable first.  A truncated/corrupt ledger must not turn a bare
        # prepare+receipt+commit sequence into a false successful beat.
        for etype, fields in final_records:
            _require_final_record(rows, etype, fields)
        return message_ids

    for etype, fields in final_records:
        if etype == "out.open":
            fields = dict(fields, beat=ledger.current_beat())
        _append_once(ledger, etype, operation_id, **fields)
    _append_once(ledger, "qa.post.commit", operation_id,
                 chunks=len(plan["chunks"]), message_ids=message_ids)
    return message_ids


def _post_under_lock(cfg, ledger, text, cue=None, kind=None, send_fn=None,
                     operation_id=None, history_fn=None, sleep_fn=time.sleep,
                     random_fn=random.random, max_attempts=MAX_ATTEMPTS,
                     retry_budget_s=MAX_RETRY_BUDGET_S):
    """Implement one post while the public entry point owns its saga lock.

    ``send_fn`` receives ``(cfg, payload_dict)``.  A caller retrying after a
    crash must reuse ``operation_id``; any missing receipt is reconciled from
    covered remote history before it can be sent.  The return status is one of
    ``disabled``, ``invalid``, ``failed``, ``partial``, or ``committed``.
    """
    if cfg.transport.get("write_enabled") is not True:
        return {"ok": False, "status": "disabled",
                "error": "posting_disabled: set transport.write_enabled=true "
                         "only after granting explicit Discord write consent"}
    try:
        plan = prepare(cfg, text, cue=cue, kind=kind,
                       operation_id=operation_id)
        if send_fn is None:
            _validate_live_config(cfg, require_token=False)
        _audit_ledger(ledger)
    except (PostError, ConfigError) as raw:
        error = raw if isinstance(raw, PostError) else PostError(
            "invalid_config", str(raw))
        return {"ok": False, "status": "invalid", "error":
                f"{error.code}: {error.detail}"}

    operation_id = plan["operation_id"]
    rows = _operation_rows(ledger, operation_id)
    prepared = _rows_of(rows, "qa.post.prepare")
    if len(prepared) > 1:
        return {"ok": False, "status": "invalid",
                "operation_id": operation_id,
                "error": "invalid_saga: duplicate prepare records"}
    resumed = bool(prepared)
    if prepared:
        expected_prepare = {
            "plan_digest": plan["plan_digest"],
            "content_digest": plan["content_digest"],
            "chars": len(plan["text"]),
            "chunks": len(plan["chunks"]),
            "cued_seat": plan["seat"].id if plan["seat"] else None,
            "pair_id": plan["pair_id"],
            "kind": kind,
            "source_text": plan["source_text"],
            "text": plan["text"],
            "payloads": plan["payloads"],
            "transport": plan["transport"],
            "mention_repaired": plan["mention_repaired"] or None,
        }
        mismatch = [key for key, value in expected_prepare.items()
                    if prepared[0].get(key) != value]
        if mismatch:
            return {"ok": False, "status": "invalid",
                    "operation_id": operation_id,
                    "error": "operation_mismatch: operation_id already names a "
                             "different or corrupt plan (" +
                             ", ".join(sorted(mismatch)) + ")"}
    else:
        if send_fn is None:
            try:
                _validate_live_config(cfg, require_token=True)
            except (PostError, ConfigError) as raw:
                error = raw if isinstance(raw, PostError) else PostError(
                    "invalid_config", str(raw))
                return {"ok": False, "status": "invalid",
                        "operation_id": operation_id,
                        "error": f"{error.code}: {error.detail}"}
        if plan["pair_id"] and any(
                row.get("type") == "out.open"
                and row.get("id") == plan["pair_id"]
                and row.get("operation_id") != operation_id
                for row in ledger.records()):
            return {"ok": False, "status": "invalid",
                    "operation_id": operation_id,
                    "error": "pair_collision: generated cue obligation ID already exists"}
        _append_once(
            ledger, "qa.post.prepare", operation_id,
            plan_digest=plan["plan_digest"],
            content_digest=plan["content_digest"], chars=len(plan["text"]),
            chunks=len(plan["chunks"]), cued_seat=(plan["seat"].id
                                                   if plan["seat"] else None),
            pair_id=plan["pair_id"], kind=kind,
            source_text=plan["source_text"], text=plan["text"],
            payloads=plan["payloads"], transport=plan["transport"],
            mention_repaired=plan["mention_repaired"] or None)

    rows = _operation_rows(ledger, operation_id)
    try:
        receipts = _validate_receipts(plan, _receipt_map(rows))
    except PostError as error:
        return {"ok": False, "status": "invalid",
                "operation_id": operation_id,
                "error": f"{error.code}: {error.detail}"}
    commits = _rows_of(rows, "qa.post.commit")
    if commits:
        try:
            ids = _finalize(ledger, plan, latency_ms=0)
        except PostError as error:
            return {"ok": False, "status": "invalid",
                    "operation_id": operation_id,
                    "error": f"{error.code}: {error.detail}"}
        return {"ok": True, "status": "committed", "operation_id": operation_id,
                "chunks": len(plan["chunks"]), "message_ids": ids,
                "mention_repaired": plan["mention_repaired"], "resumed": True}

    if resumed and len(receipts) < len(plan["chunks"]):
        if send_fn is None:
            try:
                _validate_live_config(cfg, require_token=True)
            except (PostError, ConfigError) as raw:
                error = raw if isinstance(raw, PostError) else PostError(
                    "invalid_config", str(raw))
                return {"ok": False, "status": "invalid",
                        "operation_id": operation_id,
                        "error": f"{error.code}: {error.detail}"}
        chosen_history = history_fn
        if chosen_history is None and send_fn is None:
            chosen_history = discord_history_since
        if chosen_history is None:
            return {"ok": False, "status": "invalid",
                    "operation_id": operation_id,
                    "error": "reconciliation_required: resumed operations need "
                             "covered remote history before retry"}
        try:
            receipts = _reconcile(cfg, ledger, plan, chosen_history)
        except (PostError, urllib.error.URLError, OSError, ValueError) as raw:
            error = raw if isinstance(raw, PostError) else PostError(
                "history_failed", str(raw)[:300])
            return {"ok": False, "status": "invalid",
                    "operation_id": operation_id,
                    "error": f"{error.code}: {error.detail}"}

    send = send_fn or discord_send
    t0 = time.time()
    retry_state = {"waited": 0.0}
    for index, payload in enumerate(plan["payloads"]):
        if index in receipts:
            continue
        try:
            message_id = _send_with_retry(
                send, cfg, payload, sleep_fn=sleep_fn, random_fn=random_fn,
                max_attempts=max_attempts, retry_budget_s=retry_budget_s,
                retry_state=retry_state)
        except DeliveryError as error:
            receipts = _validate_receipts(
                plan, _receipt_map(_operation_rows(ledger, operation_id)))
            _record_failure(ledger, plan, error, len(receipts))
            return {"ok": False,
                    "status": "partial" if receipts else "failed",
                    "operation_id": operation_id,
                    "error": f"{error.code}: {error.detail}",
                    "chunks": len(plan["chunks"]),
                    "chunks_sent": len(receipts),
                    "delivery_uncertain": error.delivery_uncertain}
        # Receipt immediately after each remote success.  If this append
        # crashes, resume requires remote-history reconciliation by nonce.
        receipt = _append_receipt_once(
            ledger, operation_id, index, message_id, payload["nonce"],
            reconciled=False)
        receipts[index] = receipt

    latency_ms = int((time.time() - t0) * 1000)
    try:
        ids = _finalize(ledger, plan, latency_ms)
    except PostError as error:
        return {"ok": False, "status": "invalid", "operation_id": operation_id,
                "error": f"{error.code}: {error.detail}"}
    return {"ok": True, "status": "committed", "operation_id": operation_id,
            "chunks": len(plan["chunks"]),
            "mention_repaired": plan["mention_repaired"],
            "latency_ms": latency_ms, "message_ids": ids,
            "resumed": resumed}


def post(cfg, ledger, text, cue=None, kind=None, send_fn=None,
         operation_id=None, history_fn=None, sleep_fn=time.sleep,
         random_fn=random.random, max_attempts=MAX_ATTEMPTS,
         retry_budget_s=MAX_RETRY_BUDGET_S):
    """Prepare, serialize, deliver, receipt, and commit one logical beat.

    Calls sharing a ledger and operation ID are serialized across both threads
    and processes.  Lock acquisition is bounded and the OS releases ownership
    if a process crashes; a second caller then observes the durable saga before
    deciding whether any network call is safe.
    """
    if cfg.transport.get("write_enabled") is not True:
        return {"ok": False, "status": "disabled",
                "error": "posting_disabled: set transport.write_enabled=true "
                         "only after granting explicit Discord write consent"}
    try:
        # Validate all caller-controlled plan fields before creating a lock
        # file.  The implementation repeats this inside the lock so a mutable
        # config cannot win a validation/authority race.
        plan = prepare(cfg, text, cue=cue, kind=kind,
                       operation_id=operation_id)
        if send_fn is None:
            _validate_live_config(cfg, require_token=False)
        operation_id = plan["operation_id"]
        with _saga_lock(ledger, operation_id):
            return _post_under_lock(
                cfg, ledger, text, cue=cue, kind=kind, send_fn=send_fn,
                operation_id=operation_id, history_fn=history_fn,
                sleep_fn=sleep_fn, random_fn=random_fn,
                max_attempts=max_attempts, retry_budget_s=retry_budget_s)
    except (PostError, ConfigError) as raw:
        error = raw if isinstance(raw, PostError) else PostError(
            "invalid_config", str(raw))
        result = {"ok": False, "status": "invalid",
                  "error": f"{error.code}: {error.detail}"}
        if isinstance(operation_id, str):
            result["operation_id"] = operation_id
        return result


def resume(cfg, ledger, operation_id, send_fn=None, history_fn=None,
           sleep_fn=time.sleep, random_fn=random.random,
           max_attempts=MAX_ATTEMPTS,
           retry_budget_s=MAX_RETRY_BUDGET_S):
    """Resume one prepared operation entirely from its durable ledger plan."""
    try:
        operation_id = _operation_id(operation_id)
    except PostError as error:
        return {"ok": False, "status": "invalid",
                "error": f"{error.code}: {error.detail}"}
    prepared = _rows_of(_operation_rows(ledger, operation_id),
                        "qa.post.prepare")
    if len(prepared) != 1:
        return {
            "ok": False, "status": "invalid", "operation_id": operation_id,
            "error": "recovery_plan_unavailable: resume requires exactly one "
                     "schema-valid prepare record",
        }
    row = prepared[0]
    source_text = row.get("source_text")
    if not isinstance(source_text, str) or not source_text.strip():
        return {
            "ok": False, "status": "invalid", "operation_id": operation_id,
            "error": "recovery_plan_unavailable: prepare has no complete source text",
        }
    return post(
        cfg, ledger, source_text, cue=row.get("cued_seat"),
        kind=row.get("kind"), send_fn=send_fn, operation_id=operation_id,
        history_fn=history_fn, sleep_fn=sleep_fn, random_fn=random_fn,
        max_attempts=max_attempts, retry_budget_s=retry_budget_s)
