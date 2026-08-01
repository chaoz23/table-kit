# Data safety and authority boundary

The session ledger can contain table behavior, inferred player feedback, dice
arithmetic, and—only when `--keep-text` is chosen—player prose. Treat the file
as table-private data.

## Paths and local permissions

- Relative `data_dir` values are resolved from the directory containing the
  loaded `table.json`, matching the Discord listener.
- A session is an opaque 1–128 character ID, not a path. Separators, absolute
  paths, `.` and `..` are refused, and the final ledger path must remain inside
  the canonical data directory.
- New ledger directories and files default to `0700` and `0600` on POSIX.
  Existing permissions are not silently rewritten; audit and tighten an
  existing deployment yourself.
- The writer refuses a final ledger component that is a symlink and refuses
  non-regular files. This contains common accidental and hostile redirections;
  it is not a substitute for running in a properly isolated host account.

## Durability and concurrent writers

Each append is schema-validated and JSON-encoded before the filesystem is
mutated. The writer opens with append semantics, takes an exclusive advisory
lock where the platform provides one, writes one complete line, and calls
`fsync` before acknowledging success. A crash can still leave a partial final
line; readers surface it as a line-local `_malformed` diagnostic instead of
silently dropping it or counting it.

These guarantees are for a local filesystem. Network filesystems may not
honor append, advisory-lock, or durability semantics in the same way. Use one
host-owned writer when the ledger is shared across processes or machines.

## Confidentiality

Ledgers are plaintext. Table-kit does not implement application-level
encryption or key management. Use full-disk or encrypted-volume storage,
encrypted backups, and the retention policy appropriate to the table. Do not
enable `--keep-text` unless retaining player prose is an explicit table
decision. Transport tokens remain environment variables and are never read
from config values.

## Integrity and AI-agent authority

Strict row validation detects malformed and schema-invalid records before they
can affect state, coverage, or denominators. It does **not** cryptographically
prove who wrote a well-formed record. An AI agent running with the same OS
authority as the ledger can still append or replace valid-looking events; mode
bits do not protect a file from its owner.

Accordingly, the current JSONL is evidence, not a tamper-proof audit log. A
deployment that evaluates an agent must put the durable writer and ledger
outside that agent's write authority and preserve source-native event IDs and
provenance at the host boundary. The suite-wide `TableEvent v1` envelope,
integrity chain, authority downgrade rules, migration, and rollback contract
are portfolio decisions tracked in `PORT-002` and `PORT-003`; this repository
does not silently define them ahead of that decision.

## Scale, rotation, and migration

Reads are line-size bounded and every row is checked, but current reports scan
the complete ledger. There is no built-in index, rotation, archive, or
dry-run migration command yet. Archive a completed session as an immutable
unit and begin a new opaque session ID for the next one. Do not split an active
session behind the writer's back. Native-ID indexing, bounded history reads,
versioned rotation, and migration/rollback remain explicit follow-up work.
