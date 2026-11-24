"""Live test: one command that exercises the harness against the owner's real accounts.

Contract: docs/live-test.md. The runner uses only the agent's tool layer (`App.call`) for
every action, never confirms a grant itself, and cleans up every item it makes.
"""
