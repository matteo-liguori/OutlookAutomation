"""Stable audit identity for an agent session."""


def agent_actor(session: str | None) -> str:
    return f"agent:{session or 'none'}"
