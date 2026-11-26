"""authorise() and the one write state machine; grants; the audit log.

Contract: docs/contracts/04-writes.md and 05-grants.md (binding),
08-ports.md, 09-storage.md, 02-tools.md.

Modules: repo (all SQL for grants, known_recipients, operations, audit_log),
audit (hash chain, verification, pruning), authorise (pure decision), grants
(lifecycle), transitions (the one place an operation changes state), core
(propose, do, reconcile and recover), control (cancel and revoke for
`forget`), confirm (the viewer endpoint contract, G-17 to G-19), maintenance
(what the tick calls), services (`build`), handlers (propose, do, reconcile,
doctor audit checks), render (harness-written previews).
"""
