"""The data root may not sit inside the code checkout (private data must never be committed)."""

import io
import json
from pathlib import Path

import pytest

import chamberlain
from chamberlain.connections import load_connections
from chamberlain.dataroot import code_folders, refuse_inside_checkout
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.migrate import open_store
from chamberlain.surface.cli import main

CODE = Path(chamberlain.__file__).resolve().parent.parent


def test_a_data_root_inside_the_checkout_is_refused_everywhere_it_is_opened(
    tmp_path: Path,
) -> None:
    assert CODE in code_folders()
    inside = CODE / "no-such-data-root"
    with pytest.raises(ChamberlainError) as raised:
        open_store(inside, ManualClock())
    assert raised.value.code is ErrorCode.CONFIG_INVALID
    assert "outside" in (raised.value.fix or "")
    with pytest.raises(ChamberlainError):
        load_connections(inside / "deeper")
    out = io.StringIO()
    assert main(["--data", str(inside), "doctor"], out) == 1
    assert json.loads(out.getvalue())["error"]["code"] == "config.invalid"
    assert main(["--data", str(inside), "tick"], io.StringIO()) == 1
    assert main(["--data", str(inside), "setup", "thunderbird"], io.StringIO()) == 1
    assert not inside.exists()


def test_a_git_work_tree_around_the_code_counts_even_when_the_code_is_deeper(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "work"
    (tree / ".git").mkdir(parents=True)
    package = tree / "src"
    package.mkdir()
    with pytest.raises(ChamberlainError):
        refuse_inside_checkout(tree / "notes" / "data", folders=(package, tree))
    with pytest.raises(ChamberlainError):
        refuse_inside_checkout(tree, folders=(package, tree))
    assert refuse_inside_checkout(tmp_path / "beside", folders=(package, tree)) == (
        tmp_path / "beside"
    )
    assert refuse_inside_checkout(tmp_path / "work-data", folders=(package, tree))
