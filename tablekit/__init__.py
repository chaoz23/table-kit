"""table-kit — the glue for running a live table where some seats are agents.

Three deterministic referees already exist for the *rules* of such a table
(`srdcheck` for verdicts, `charactercheck` for sheets, `dmcheck` for conduct).
This package is the layer underneath them: the transport, the session file,
and the instrumentation that makes an evening of play into something you can
learn from afterwards.

Start at `tablekit.events` — the one-file event stream is the design, and
everything else reads or writes it.
"""

from .cli import __version__, main
from .config import TableConfig, load
from .events import Ledger, make, validate
from .legacy_events import (LegacyMigration, LegacyMigrationError,
                            migrate_ledger, migrate_records)

__all__ = ["Ledger", "TableConfig", "load", "main", "make", "validate",
           "LegacyMigration", "LegacyMigrationError", "migrate_ledger",
           "migrate_records", "__version__"]
