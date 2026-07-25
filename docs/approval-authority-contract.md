# Approval authority contract

P2-013A is the approved entry milestone for the RM-013 approval-authority
contract. It is in progress.

The approved boundary covers actor verification, authority scopes, canonical
action and request digests, decision idempotency, expected-revision CAS,
all-or-nothing validation, strict V2 ledgers, bounded security-failure audit,
V1 compatibility projection, and an unavailable external-execution binding.

P2-013A does not implement approval lifecycle or reissue, the P2-027B cleanup
executor, P2-028A isolation, an external provider or solver, automatic retry,
or parallel scheduling. Those remain separately approval-gated.
