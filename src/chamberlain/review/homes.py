"""Where the agents keep their session transcripts and settings.

Contract: docs/contracts/06-memory.md (M-40). Every path to a home folder comes from here, so a
test can point all of it at an empty folder with `CHAMBERLAIN_AGENT_HOME`. Under pytest the
variable is required: reading the real `~/.claude` or `~/.codex` from a test is a bug.
"""

import os
from dataclasses import dataclass
from pathlib import Path

HOME_ENV = "CHAMBERLAIN_AGENT_HOME"
"""A folder that stands in for the home folder: it holds `.claude` and `.codex`."""


@dataclass(frozen=True)
class AgentHomes:
    claude: Path
    """Claude Code's configuration folder (settings, and `projects/` with the transcripts)."""
    codex: Path
    """Codex's home (`sessions/` holds the transcripts)."""

    @property
    def claude_transcripts(self) -> Path:
        return self.claude / "projects"

    @property
    def codex_transcripts(self) -> Path:
        return self.codex / "sessions"


def agent_homes() -> AgentHomes:
    override = os.environ.get(HOME_ENV)
    if override:
        base = Path(override)
        return AgentHomes(base / ".claude", base / ".codex")
    if "PYTEST_CURRENT_TEST" in os.environ:
        raise RuntimeError(f"a test reached the real agent folders; set {HOME_ENV}")
    claude = os.environ.get("CLAUDE_CONFIG_DIR")
    codex = os.environ.get("CODEX_HOME")
    return AgentHomes(
        Path(claude) if claude else Path.home() / ".claude",
        Path(codex) if codex else Path.home() / ".codex",
    )
