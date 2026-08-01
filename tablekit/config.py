"""Table configuration. Everything table-specific lives here, nothing in code.

The previous generation of this kit was a shell script with a channel ID, a
bot ID, three absolute paths and one collaborator's mention string baked into
it. It ran one table very well and could not run a second one at all. So the
first rule of this file is that the code below contains no identifiers.

## Fail closed before the first beat

An **agent seat must declare a `mention`**, and it must be the literal string
its chat platform requires (on Discord, `<@1234567890>`).

This looks like pedantry until it costs you an evening. Agent frameworks
commonly gate bot-to-bot delivery behind "only if mentioned", so a cue that
addresses an agent seat by name — the in-fiction cue that works perfectly for
every human at the table — is accepted by the API, shows up in the channel,
and is never delivered. Nothing errors. The seat simply goes quiet, and the
GM concludes the agent is thinking.

A config that would fail that way is refused at load. The loader also rejects
unknown fields, bad types, ambiguous identity keys, unsafe session IDs, and
non-finite thresholds. These are not style preferences: an AI agent is the
primary operator, so a typo must become an actionable refusal rather than an
alternate ledger or a plausible-looking wrong attribution.
"""

import json
import math
import os
import re
import unicodedata

REQUIRED = ("name", "seats")

#: Defaults chosen from measurement where measurement exists, and marked
#: where it does not. See docs/INSTRUMENTATION.md for provenance.
DEFAULT_THRESHOLDS = {
    # A seat with no line for this long is quiet enough to check on. Derived
    # from live failure, not corpus: a seat went 40 minutes unaddressed while
    # the rest of the table carried a scene to its climax.
    "seat_quiet_s": 600,
    # How long a cue may stand before it is treated as unanswered. Judgment.
    "cue_ttl_s": 300,
    # How long a called-for roll may stand unconsumed before it is a finding.
    "roll_ttl_s": 300,
    # A GM beat longer than this is worth a look — not wrong, but it should be
    # paying something off. Professional median is 8-14 words; the long tail is
    # about one line in six.
    "long_beat_words": 120,
    # More than this many chat messages for one beat is a transport smell: the
    # table reads it as several beats and answers the first one.
    "max_chunks": 2,
}


class ConfigError(ValueError):
    pass


#: Who throws the dice for a seat. `self` is the default and the right one for
#: almost everyone — taking the dice off a player removes the best moment in
#: the game. `dm` is an opt-out that matters to a real minority: accessibility,
#: players who find being put on the spot stressful, and players who are here
#: for the story and not the arithmetic.
#:
#: It lives here rather than in the GM's memory because a preference the GM has
#: to remember gets forgotten, and forgetting it is invisible — the player is
#: simply put on the spot and says nothing about it.
ROLL_MODES = ("self", "dm")

TOP_LEVEL_KEYS = {
    "name", "seats", "gm", "transport", "data_dir", "thresholds", "session",
}
SEAT_KEYS = {
    "id", "display", "kind", "mention", "aliases", "player", "rolls",
    "sheet_id",
}
TRANSPORT_KEYS = {
    "kind", "channel_id", "bot_user_id", "token_env", "roll_relay_bots",
    "write_enabled",
}
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def normalize_identity(value):
    """Normalize an identity token without turning it into a substring.

    NFKC handles canonically equivalent and compatibility Unicode spellings;
    casefold handles non-ASCII case; whitespace folding prevents a platform's
    display formatting from creating a second identity.  The resulting token
    still has to match in full: ``Will`` never matches ``William``.
    """
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return " ".join(normalized.split())


def _unknown_keys(data, allowed, path):
    unknown = sorted(str(k) for k in data
                     if k not in allowed and not str(k).startswith("_"))
    if unknown:
        raise ConfigError(
            f"{path}: unknown field(s) {', '.join(unknown)}; remove a typo or "
            "prefix comment-only metadata with '_'")


def _text(value, path, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: expected a non-empty string")
    return value.strip()


def _number(value, path, minimum=0, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        kind = "integer" if integer else "number"
        raise ConfigError(f"{path}: expected a finite {kind}")
    if not math.isfinite(value):
        raise ConfigError(f"{path}: expected a finite number")
    if integer and not isinstance(value, int):
        raise ConfigError(f"{path}: expected an integer")
    if value < minimum:
        raise ConfigError(f"{path}: must be at least {minimum}")
    return value


def _session_id(value, path="session"):
    value = _text(value, path)
    if not SESSION_RE.fullmatch(value):
        raise ConfigError(
            f"{path}: use 1-128 ASCII letters, digits, '.', '_' or '-', "
            "starting with a letter or digit; path separators are not allowed")
    return value


class Seat:
    __slots__ = ("id", "display", "kind", "mention", "aliases", "player",
                 "rolls", "sheet_id")

    def __init__(self, d, path="seat"):
        if not isinstance(d, dict):
            raise ConfigError(f"{path}: expected an object")
        _unknown_keys(d, SEAT_KEYS, path)
        self.id = _text(d.get("id"), f"{path}.id")
        self.display = _text(d.get("display", self.id), f"{path}.display")
        self.kind = _text(d.get("kind", "human"), f"{path}.kind")
        if self.kind not in ("human", "agent", "gm"):
            raise ConfigError(
                f"{path}.kind: must be human, agent or gm (got {self.kind!r})")
        self.mention = _text(d.get("mention"), f"{path}.mention", optional=True)
        aliases = d.get("aliases", [])
        if not isinstance(aliases, list):
            raise ConfigError(f"{path}.aliases: expected a list of strings")
        self.aliases = [normalize_identity(_text(a, f"{path}.aliases[{i}]"))
                        for i, a in enumerate(aliases)]
        if len(self.aliases) != len(set(self.aliases)):
            raise ConfigError(
                f"{path}.aliases: duplicate aliases are ambiguous; keep each once")
        redundant = ({normalize_identity(self.id), normalize_identity(self.display)}
                     & set(self.aliases))
        if redundant:
            raise ConfigError(
                f"{path}.aliases: {sorted(redundant)[0]!r} duplicates this "
                "seat's id/display; remove the redundant identity")
        self.player = _text(d.get("player"), f"{path}.player", optional=True)
        # Exact attribution key for relayed rolls. A character sheet id cannot
        # be ambiguous the way a display name can, and real character names
        # carry decoration the seat name will never match exactly.
        sheet_id = d.get("sheet_id")
        if (isinstance(sheet_id, bool)
                or (sheet_id is not None and not isinstance(sheet_id, (str, int)))):
            raise ConfigError(f"{path}.sheet_id: expected a non-empty string or integer")
        if isinstance(sheet_id, str):
            sheet_id = _text(sheet_id, f"{path}.sheet_id")
        self.sheet_id = sheet_id
        self.rolls = _text(d.get("rolls", "self"), f"{path}.rolls")
        if self.rolls not in ROLL_MODES:
            raise ConfigError(
                f"{path}.rolls: must be one of {', '.join(ROLL_MODES)} "
                f"(got {self.rolls!r})")
        if self.kind == "agent" and not self.mention:
            raise ConfigError(
                f"{path} ({self.id}) is an agent seat with no 'mention'. Agent chat "
                "bridges commonly drop bot-to-bot messages that lack a literal "
                "mention, so cues to this seat would be accepted, posted, and "
                "never delivered — with no error anywhere. Set 'mention' to the "
                "platform's literal form, e.g. \"<@1234567890>\".")

    def matches(self, name):
        n = normalize_identity(name)
        return n in ({normalize_identity(self.id), normalize_identity(self.display)}
                     | set(self.aliases))

    def as_dict(self):
        return {"id": self.id, "display": self.display, "kind": self.kind,
                "mention": self.mention, "aliases": self.aliases,
                "player": self.player, "rolls": self.rolls,
                "sheet_id": self.sheet_id}

    @property
    def rolls_own(self):
        return self.rolls == "self"


class TableConfig:
    def __init__(self, data, path=None):
        if not isinstance(data, dict):
            raise ConfigError("config: expected a JSON object")
        _unknown_keys(data, TOP_LEVEL_KEYS, "config")
        self.path = (os.path.realpath(os.path.abspath(os.path.expanduser(path)))
                     if path else None)
        self.root = os.path.dirname(self.path) if self.path else os.getcwd()
        missing = [k for k in REQUIRED if k not in data]
        if missing:
            raise ConfigError(f"config missing required key(s): {', '.join(missing)}")
        self.name = _text(data["name"], "config.name")
        self.raw = data
        if not isinstance(data["seats"], list):
            raise ConfigError("config.seats: expected a list of seat objects")
        self.seats = [Seat(s, f"config.seats[{i}]")
                      for i, s in enumerate(data["seats"])]
        gm = data.get("gm")
        if gm is not None and not isinstance(gm, dict):
            raise ConfigError("config.gm: expected a seat object")
        if gm is not None and gm.get("kind", "gm") != "gm":
            raise ConfigError("config.gm.kind: must be 'gm'")
        self.gm = (Seat({**gm, "kind": "gm"}, "config.gm")
                   if gm is not None else None)

        transport = data.get("transport", {})
        if not isinstance(transport, dict):
            raise ConfigError("config.transport: expected an object")
        _unknown_keys(transport, TRANSPORT_KEYS, "config.transport")
        self.transport = dict(transport)
        for key in ("kind", "channel_id", "bot_user_id", "token_env"):
            if key in transport:
                self.transport[key] = _text(
                    transport[key], f"config.transport.{key}")
        relays = transport.get("roll_relay_bots", [])
        if not isinstance(relays, list):
            raise ConfigError(
                "config.transport.roll_relay_bots: expected a list of strings")
        self.transport["roll_relay_bots"] = [
            _text(relay, f"config.transport.roll_relay_bots[{i}]")
            for i, relay in enumerate(relays)
        ]
        relay_keys = [normalize_identity(relay).replace(" ", "")
                      for relay in self.transport["roll_relay_bots"]]
        if len(relay_keys) != len(set(relay_keys)):
            raise ConfigError(
                "config.transport.roll_relay_bots: duplicate normalized relay "
                "identities are ambiguous; keep each once")
        write_enabled = transport.get("write_enabled", False)
        if not isinstance(write_enabled, bool):
            raise ConfigError("config.transport.write_enabled: expected a boolean")
        self.transport["write_enabled"] = write_enabled

        raw_data_dir = _text(data.get("data_dir", "./table-data"),
                             "config.data_dir")
        raw_data_dir = os.path.expanduser(raw_data_dir)
        if not os.path.isabs(raw_data_dir):
            raw_data_dir = os.path.join(self.root, raw_data_dir)
        self.data_dir = os.path.realpath(os.path.abspath(raw_data_dir))

        supplied_thresholds = data.get("thresholds", {})
        if not isinstance(supplied_thresholds, dict):
            raise ConfigError("config.thresholds: expected an object")
        _unknown_keys(supplied_thresholds, set(DEFAULT_THRESHOLDS),
                      "config.thresholds")
        self.thresholds = {**DEFAULT_THRESHOLDS, **supplied_thresholds}
        for key, value in self.thresholds.items():
            integer = key in ("long_beat_words", "max_chunks")
            minimum = 1 if integer else 0
            _number(value, f"config.thresholds.{key}", minimum, integer)

        if not self.seats:
            raise ConfigError("a table needs at least one seat")
        if "session" in data:
            _session_id(data["session"], "config.session")
        self._validate_identities()

    def _validate_identities(self):
        identities = {}
        mentions = {}
        sheets = {}
        seats = self.seats + ([self.gm] if self.gm else [])
        for seat in seats:
            tokens = {normalize_identity(seat.id),
                      normalize_identity(seat.display), *seat.aliases}
            for token in tokens:
                owner = identities.get(token)
                if owner is not None and owner is not seat:
                    raise ConfigError(
                        f"config identity {token!r} matches both {owner.id!r} and "
                        f"{seat.id!r}; rename the ID/display/alias so lookups are unique")
                identities[token] = seat
            if seat.mention:
                owner = mentions.get(seat.mention)
                if owner is not None:
                    raise ConfigError(
                        f"config mention {seat.mention!r} is shared by {owner.id!r} "
                        f"and {seat.id!r}; give each seat its literal unique mention")
                mentions[seat.mention] = seat
            if seat.sheet_id is not None:
                sheet = str(seat.sheet_id)
                owner = sheets.get(sheet)
                if owner is not None:
                    raise ConfigError(
                        f"config sheet_id {sheet!r} is shared by {owner.id!r} and "
                        f"{seat.id!r}; source-native sheet IDs must identify one seat")
                sheets[sheet] = seat

    # ---- lookups ----------------------------------------------------
    def seat(self, name):
        for s in self.seats:
            if s.matches(name):
                return s
        if self.gm and self.gm.matches(name):
            return self.gm
        return None

    def is_gm(self, name):
        return bool(self.gm and self.gm.matches(name))

    @property
    def player_seats(self):
        return [s for s in self.seats if s.kind != "gm"]

    def ledger_path(self, session=None):
        session = _session_id(session or self.raw.get("session", "session"))
        candidate = os.path.join(self.data_dir, f"{session}.jsonl")
        if os.path.islink(candidate):
            raise ConfigError(
                f"session {session!r} resolves through a ledger symlink; remove "
                "the link and use a regular file inside data_dir")
        resolved = os.path.realpath(candidate)
        if os.path.commonpath((self.data_dir, resolved)) != self.data_dir:
            raise ConfigError(
                f"session {session!r} resolves outside data_dir; choose an opaque "
                "session ID, not a path")
        return candidate

    def token(self):
        """Read the transport secret from the environment, never from config.

        A token in a config file is a token in a backup, a paste, and
        eventually a repository.
        """
        var = self.transport.get("token_env", "TABLE_BOT_TOKEN")
        tok = os.environ.get(var)
        if not tok:
            raise ConfigError(
                f"transport token not found: set ${var} in the environment "
                "(tokens are never read from the config file)")
        return tok

    def mention_check(self, text, seat):
        """Would a cue to `seat` in `text` actually be delivered?

        Returns None if fine, or an explanation. Only agent seats can fail —
        humans read their own name.
        """
        if not seat or seat.kind != "agent" or not seat.mention:
            return None
        if seat.mention in (text or ""):
            return None
        return (f"cue addresses agent seat '{seat.display}' but does not "
                f"contain its literal mention {seat.mention} — likely to be "
                "dropped in transit with no error")


def load(path=None):
    """Load a table config.

    Search order: explicit path, `$TABLE_CONFIG`, `./table.json`.
    """
    path = path or os.environ.get("TABLE_CONFIG") or "table.json"
    path = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if not os.path.exists(path):
        raise ConfigError(
            f"no table config at {path}. Copy examples/table.example.json to "
            "table.json and edit it, or set $TABLE_CONFIG.")
    try:
        f = open(path)
    except OSError as e:
        raise ConfigError(f"cannot read table config {path}: {e}") from e
    with f:
        try:
            data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            raise ConfigError(f"{path} is not readable valid JSON: {e}") from e
    return TableConfig(data, path=path)
