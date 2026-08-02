# Live-agent trust boundary threat model

## Assets and principals

Protected assets are observed source events, source sequence/cursors, roster
and role assignments, charter/rules/source-set configuration, participant
consent and visibility, evaluator inputs/results, and host credentials.

Principals are: participants, the GM, an evaluated agent, transport adapters,
deterministic evaluators, the suite host/operator, and storage/OS administrators.
The local filesystem and a process sharing the evaluated agent's credentials
are not independent trust zones.

## Required capability split

| Operation | Agent | Adapter | Evaluator | Host/operator |
| --- | --- | --- | --- | --- |
| Propose dialogue/action | allow | no | no | allow |
| Observe source-native event | no | allow | read | read |
| Change roster/policy/consent | no | no | no | allow |
| Advance ingestion cursor | no | propose | no | commit |
| Emit finding calculation | no | no | allow | record |
| Mark evaluation complete/attested | no | no | propose | commit |
| Post to table | scoped proposal only | scoped delivery | no | authorize |

Read-only shadow mode grants the agent no posting credential. Capability
failures are typed authorization failures, never silent skips.

## Abuse cases and controls

- **Forged `qc.run` or result:** the host stores evaluator output outside the
  agent-writable ledger and binds it to the input/config digests.
- **Roster/policy override:** only a host capability may version the protected
  session descriptor; results echo its digest.
- **Source spoof/replay:** the authenticated adapter preserves native IDs and
  sequence, deduplicates them, and advances a host-owned cursor atomically.
- **Ledger edit:** a same-writer hash is only `self_attested`; an external key
  or checkpoint is required for `host_attested`. Mismatch downgrades/refuses.
- **Prompt/content injection:** source content remains data; it cannot grant a
  capability, mutate policy, or mark evaluation complete.
- **Inference impersonating a participant:** inferred, reported, observed, and
  decided provenance remain distinct; corrections are a reserved typed family.
- **Restart/partial commit:** host recovery resumes from durable cursor plus
  idempotency keys and refuses gaps or digest mismatch.

## Residual constraints

An operator or OS administrator with access to both host keys and storage can
still rewrite evidence. Single-process deployments cannot claim independent
authority. Participant consent, deletion, export, and correction workflows are
release gates beyond this contract foundation.
