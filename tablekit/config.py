"""Table configuration. Everything table-specific lives here, nothing in code.

The previous generation of this kit was a shell script with a channel ID, a
bot ID, three absolute paths and one collaborator's mention string baked into
it. It ran one table very well and could not run a second one at all. So the
first rule of this file is that the code below contains no identifiers.

## The one rule the loader actually enforces

An **agent seat must declare a `mention`**, and it must be the literal string
its chat platform requires (on Discord, `<@1234567890>`).

This looks like pedantry until it costs you an evening. Agent frameworks
commonly gate bot-to-bot delivery behind "only if mentioned", so a cue that
addresses an agent seat by name — the in-fiction cue that works perfectly for
every human at the table — is accepted by the API, shows up in the channel,
and is never delivered. Nothing errors. The seat simply goes quiet, and the
GM concludes the agent is thinking.

A config that would fail that way is refused at load, because it is the one
defect in this system with no symptom at run time.
"""

import json
import os

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


class Seat:
    __slots__ = ("id", "display", "kind", "mention", "aliases", "player",
                 "rolls")

    def __init__(self, d):
        self.id = d.get("id")
        if not self.id:
            raise ConfigError("every seat needs an 'id'")
        self.display = d.get("display", self.id)
        self.kind = d.get("kind", "human")
        if self.kind not in ("human", "agent", "gm"):
            raise ConfigError(
                f"seat {self.id}: kind must be human, agent or gm (got {self.kind!r})")
        self.mention = d.get("mention")
        self.aliases = [a.lower() for a in d.get("aliases", [])]
        self.player = d.get("player")
        self.rolls = d.get("rolls", "self")
        if self.rolls not in ROLL_MODES:
            raise ConfigError(
                f"seat {self.id}: rolls must be one of {', '.join(ROLL_MODES)} "
                f"(got {self.rolls!r})")
        if self.kind == "agent" and not self.mention:
            raise ConfigError(
                f"seat {self.id} is an agent seat with no 'mention'. Agent chat "
                "bridges commonly drop bot-to-bot messages that lack a literal "
                "mention, so cues to this seat would be accepted, posted, and "
                "never delivered — with no error anywhere. Set 'mention' to the "
                "platform's literal form, e.g. \"<@1234567890>\".")

    def matches(self, name):
        n = (name or "").strip().lower()
        return n in ({self.id.lower(), self.display.lower()} | set(self.aliases))

    def as_dict(self):
        return {"id": self.id, "display": self.display, "kind": self.kind,
                "mention": self.mention, "aliases": self.aliases,
                "player": self.player, "rolls": self.rolls}

    @property
    def rolls_own(self):
        return self.rolls == "self"


class TableConfig:
    def __init__(self, data, path=None):
        self.path = path
        missing = [k for k in REQUIRED if k not in data]
        if missing:
            raise ConfigError(f"config missing required key(s): {', '.join(missing)}")
        self.name = data["name"]
        self.raw = data
        self.seats = [Seat(s) for s in data["seats"]]
        gm = data.get("gm")
        self.gm = Seat({**gm, "kind": "gm"}) if gm else None
        self.transport = data.get("transport", {})
        self.data_dir = os.path.expanduser(data.get("data_dir", "./table-data"))
        self.thresholds = {**DEFAULT_THRESHOLDS, **data.get("thresholds", {})}
        if not self.seats:
            raise ConfigError("a table needs at least one seat")

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
        session = session or self.raw.get("session", "session")
        return os.path.join(self.data_dir, f"{session}.jsonl")

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
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        raise ConfigError(
            f"no table config at {path}. Copy examples/table.example.json to "
            "table.json and edit it, or set $TABLE_CONFIG.")
    with open(path) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"{path} is not valid JSON: {e}")
    return TableConfig(data, path=path)
