"""`chamberlain demo init <data root>`: a demo-only profile in an empty folder."""

import io
import json
from pathlib import Path

from chamberlain.connections import load_connections, open_app
from chamberlain.surface import cli


def _run(*argv: str) -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    code = cli.main(list(argv), out)
    return code, json.loads(out.getvalue())


def test_demo_init_writes_a_demo_only_profile_into_a_and_3_related_checks(
    tmp_path: Path,
) -> None:
    # demo_init_writes_a_demo_only_profile_into_a_new_folder
    root = tmp_path / "demo"
    code, shown = _run("demo", "init", str(root))
    assert code == cli.EXIT_OK
    assert shown["demo"] is True
    kinds = {s.kind for s in load_connections(root).sources.values()}
    assert kinds == {"demo"}
    app = open_app(root)
    try:
        assert app.call("start", {}).ok
    finally:
        app.close()

    # demo_init_accepts_an_existing_empty_folder
    (tmp_path / "empty").mkdir()
    code, _ = _run("demo", "init", str(tmp_path / "empty"))
    assert code == cli.EXIT_OK

    # demo_init_refuses_a_folder_that_holds_anything
    keep = tmp_path / "real"
    keep.mkdir()
    (keep / "notes.txt").write_text("mine", encoding="utf-8")
    code, reply = _run("demo", "init", str(keep))
    assert code == cli.EXIT_FAILED
    assert reply["error"]["code"] == "config.invalid"  # type: ignore[index]
    assert [p.name for p in keep.iterdir()] == ["notes.txt"]

    # the_readme_documents_the_demo_command
    readme = (Path(__file__).parents[2] / "README.md").read_text(encoding="utf-8")
    assert "## Try it without real accounts" in readme
    assert "demo init" in readme
