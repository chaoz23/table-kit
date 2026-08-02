# ADR 0001: contract ownership and host boundary

Status: accepted for the 1.0 contract foundation, 2026-08-01.

## Decision

1. table-kit temporarily packages the canonical `table.evaluation/1.0` and
   `table.event/1.0` artifacts because it owns the current session ledger and
   the cross-tool RFCs.
2. table-kit does not become the trusted suite host. Its README promise is an
   editable example, and combining adapter, evaluator, transport, credential,
   and authority ownership would erase the trust boundary.
3. Host enforcement belongs in a future dedicated host package/process with a
   separate credential and storage boundary. Creating that package/repository
   is a separate product decision; this ADR fixes its responsibilities first.
4. Until that host exists, all current events and results are
   `self_attested`. A same-writer hash chain may detect accidental corruption
   but cannot upgrade authority.
5. dmcheck consumes TableEvent through an explicit adapter in the next slice.
   charactercheck and srdcheck expose joinable result references without being
   expanded into live-session consumers.

## Consequences

The contract can stabilize and ship with table-kit without falsely advertising
a control plane. The future host must own the session descriptor, credentials,
source identity, durable cursors, policy/config digests, and attestation. It may
request evaluator work, but an evaluated agent cannot write observed events or
authoritative evaluation completion.

This adds a temporary packaging dependency on table-kit. Once a host package is
approved, the identical contract artifacts move there or to a neutral contract
package under a compatibility-preserving import/CLI alias.
