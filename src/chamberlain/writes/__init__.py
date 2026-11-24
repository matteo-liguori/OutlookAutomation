"""authorise() and the one write state machine; grants; the audit log.

Contract: docs/contracts/04-writes.md and 05-grants.md (binding),
08-ports.md, 09-storage.md, 02-tools.md.
Brief: docs/waves/wave-1/c-writes.md. Owner: wave-1 track (c).

Planned modules: repo.py (SQL for grants, operations, known_recipients,
audit_log), authorise.py (pure), engine.py (the state machine), grants.py
(lifecycle and review dates), audit.py (hash chain), limits.py (send
limits), confirm.py (the viewer confirmation endpoint, G-17 to G-19),
services.py (AuditLog, WriteControl), handlers.py (propose, do, reconcile).
"""
