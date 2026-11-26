"""Release notes come from local annotated tags; reading them leaves Git untouched."""

import subprocess
from pathlib import Path

import pytest

from chamberlain.runs.updates import release_notes
from chamberlain.schema.errors import ChamberlainError, ErrorCode


def _git(cwd: Path, *args: str, data: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=data,
        capture_output=True,
        encoding="utf-8",
        timeout=8,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("prefix", ["chamberlain/v", "harness-update/v"])
def test_release_notes_read_exact_local_annotations(tmp_path: Path, prefix: str) -> None:
    tag = prefix + "12.1.0"
    _git(tmp_path, "init", "--quiet")
    blob = _git(tmp_path, "hash-object", "-w", "--stdin", data="synthetic")
    _git(
        tmp_path,
        "-c",
        "user.name=Release Fixture",
        "-c",
        "user.email=fixture@example.org",
        "tag",
        "-a",
        tag,
        "-m",
        "Release summary",
        "-m",
        "An individual change.",
        blob,
    )
    lightweight = prefix + "12.1.1"
    _git(tmp_path, "tag", lightweight, blob)
    assert release_notes(tag, cwd=tmp_path) == "Release summary\n\nAn individual change."
    assert release_notes(lightweight, cwd=tmp_path) is None
    assert release_notes(prefix + "12.1.2", cwd=tmp_path) is None

    with pytest.raises(ChamberlainError) as invalid:
        release_notes("refs/tags/*", cwd=tmp_path)
    assert invalid.value.code is ErrorCode.REQUEST_INVALID
