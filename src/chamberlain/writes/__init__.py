"""authorise() and the one write state machine; grants; the audit log.

Contract: docs/contracts/04-writes.md and 05-grants.md (binding),
08-ports.md, 09-storage.md, 02-tools.md; board rulings R1 to R7 and R11.
Brief: docs/waves/wave-1/c-writes.md. Owner: wave-1 track (c).

Modules: repo (all SQL for grants, known_recipients, operations, audit_log),
audit (hash chain, verification, pruning), authorise (pure decision), grants
(lifecycle), transitions (the one place an operation changes state), engine
(propose, do, reconcile, recover, resolve), control (cancel and revoke for
`forget`), confirm (the viewer endpoint contract, G-17 to G-19), maintenance
(what the tick calls), services (`build`), handlers (propose, do, reconcile,
doctor audit checks), render (harness-written previews).
"""
