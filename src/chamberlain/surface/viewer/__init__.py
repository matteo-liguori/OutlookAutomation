"""The local viewer: the person's window into the harness and the only place a human confirms.

Contract: docs/contracts/05-grants.md (G-14, G-17 to G-19: the confirmation channel the writes
track built in `writes/confirm.py` and this package mounts), docs/contracts/07-attention.md
(`user.note`), docs/contracts/06-memory.md (read-only views) and docs/contracts/10-targets.md
(storage figures). Loopback only, a random per-launch token, Host and Origin checks, no
third-party assets. Nothing an agent can call reaches an endpoint here: the token lives in
memory and in a user-only file, and the CLI never prints it.
"""
