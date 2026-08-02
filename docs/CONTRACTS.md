# Suite contracts

This repository temporarily publishes two transport-neutral contracts used to
make the four-tool suite joinable. They are data contracts, not a claim that
table-kit is the suite host.

## `table.evaluation/1.0`

Every evaluator can eventually project its native result into one envelope.
The status is the authority-bearing answer; prose and process success are not.

- Exit 0 is only `checked_clean` with complete coverage, no gaps, findings,
  advisories, or errors.
- Complete findings or advisories exit 1.
- Incomplete, unsupported, invalid, and internal-error results exit 2 and
  contain a typed error.
- Global counts never replace the per-evaluator inventory.
- Every finding/advisory binds its evidence IDs and effective policy.
- `self_attested` is honest but not independent. Only a future protected host
  may emit `host_attested`.

The package includes a valid fixture for every status and both authority
states. Run `tablekit contract evaluation` to print the schema.

## `table.event/1.0`

TableEvent provides immutable join keys, a contiguous canonical session
sequence, ordered source provenance, explicit
principal/controller roles, correlation/causation, visibility, sensitivity,
and honest integrity state. The initial families are session, observed
message/delivery/gap, turn/action, roll, narration obligation/observation, and
evaluation.

Consent, feedback, correction, ruling, and character/rules state are reserved
for a later compatible design. A v1 consumer must not reinterpret an unknown
type as a known one: it returns `unsupported` with a structured skip/error.

The fixture deliberately includes a clean roll→obligation→narration path, a
legal Reaction outside the current actor's normal turn, a transport gap, an
unanswered player question represented by a conduct finding, and explicit
session closure. Run `tablekit contract event` or `tablekit contract golden`.

## Compatibility and migration

- Readers support their current minor and the immediately previous minor once
  a second minor exists. Version 1.0 has no previous minor.
- Unknown major versions are refused as `unsupported`.
- Unknown event types in a future minor are preserved as opaque input and
  reported as structured unsupported coverage; they never crash or disappear.
- Current unversioned table-kit rows remain the runtime storage format until an
  explicit adapter lands. Native IDs and sequence are provenance, not
  cryptographic authentication.
- charactercheck and srdcheck remain pre-session/query evaluators. A host joins
  their results through session/entity/correlation references; neither tool is
  required to consume a live TableEvent stream.

Python and TypeScript declarations are deterministically regenerated from the
schema enums with `python scripts/generate_contract_types.py`; CI rejects stale
artifacts. Runtime remains dependency-free. JSON Schema validation is a test
and integration concern.
