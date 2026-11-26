"""The proof map's test references point to runnable test cases."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKLIST = ROOT / "docs" / "capability-checklist.md"
REFERENCE = re.compile(r"`(tests/[\w/]+\.py)(?:::(test_\w+))?`")


def test_capability_checklist_test_references_are_current() -> None:
    checklist = CHECKLIST.read_text(encoding="utf-8")
    assert re.findall(r"^## C(\d+) ", checklist, re.MULTILINE) == [
        str(number) for number in range(1, 18)
    ]
    references = set(REFERENCE.findall(checklist))
    assert references
    for filename, test_name in sorted(references):
        path = ROOT / filename
        assert path.is_file(), filename
        if test_name:
            declarations = ast.parse(path.read_text(encoding="utf-8")).body
            assert any(
                isinstance(declaration, ast.FunctionDef | ast.AsyncFunctionDef)
                and declaration.name == test_name
                for declaration in declarations
            ), f"{filename}::{test_name}"
