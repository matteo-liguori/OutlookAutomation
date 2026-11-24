"""PRINCIPLE 2. Records, evidence, artifacts, owners, leases and the one sweeper.

Contract: docs/contracts/06-memory.md (binding), 09-storage.md, 02-tools.md.
Brief: docs/waves/wave-1/a-memory.md. Owner: wave-1 track (a).

Modules: repo (all SQL), cards, evidence, refs, artifacts (content-addressed store,
owners, leases, budgets), retrieval (paging and cursors), find, reading (`open`),
sweeper (the one lifetime rule table), backups, restore, metrics, checks,
services (`build` -> ArtifactStore, EvidenceStore, RefResolver), handlers.
"""
