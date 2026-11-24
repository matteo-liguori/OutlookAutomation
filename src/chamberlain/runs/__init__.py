"""Scheduled runs, the schedule record, reports and update notices.

Contract: docs/contracts/07-attention.md (`schedule.pass_due`, `user.note`,
`update.available`, A-10 and R10), docs/contracts/06-memory.md (run-owned
artifacts, M-4) and docs/contracts/09-storage.md (migrations 0050-0059). The runs
track writes `schedules`, `runs` and `user_notes`; attention reads them to raise
events and never the other way round.
"""
