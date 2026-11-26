"""Layering and repository rules: docs/contracts/01-domain.md (D-9 to D-13).

These tests guard the domain, storage and provider import boundaries.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "chamberlain"

FORBIDDEN: dict[str, tuple[str, ...]] = {
    "domain": (
        "chamberlain.connectors",
        "chamberlain.store",
        "chamberlain.app",
        "chamberlain.memory",
        "chamberlain.attention",
        "chamberlain.writes",
        "chamberlain.surface",
        "chamberlain.ports",
        "sqlite3",
    ),
    "schema": (
        "chamberlain.connectors",
        "chamberlain.store",
        "chamberlain.app",
        "chamberlain.memory",
        "chamberlain.attention",
        "chamberlain.writes",
        "chamberlain.surface",
        "chamberlain.ports",
        "sqlite3",
    ),
    "ports": ("chamberlain.connectors", "chamberlain.store", "chamberlain.app", "sqlite3"),
    "memory": ("chamberlain.connectors", "chamberlain.surface"),
    "attention": ("chamberlain.connectors", "chamberlain.surface"),
    "writes": ("chamberlain.connectors", "chamberlain.surface"),
    "connectors": (
        "sqlite3",
        "chamberlain.store",
        "chamberlain.app",
        "chamberlain.memory",
        "chamberlain.attention",
        "chamberlain.writes",
        "chamberlain.surface",
    ),
    "store": (
        "chamberlain.connectors",
        "chamberlain.app",
        "chamberlain.memory",
        "chamberlain.attention",
        "chamberlain.writes",
        "chamberlain.surface",
    ),
}

EXPECTED_PACKAGES = (
    "domain",
    "schema",
    "ports",
    "store",
    "app",
    "memory",
    "attention",
    "writes",
    "connectors/fake",
    "connectors/thunderbird",
    "connectors/google_calendar",
    "connectors/caldav",
    "surface",
    "surface/viewer",
    "importer",
    "guidance",
)


def _imports(path: Path) -> list[tuple[str, int, bool]]:
    """`(module, line, at_top_level)` for every import in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top_level = {id(node) for node in tree.body}
    found: list[tuple[str, int, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno, id(node) in top_level) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, node.lineno, id(node) in top_level))
    return found


def _files(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


def _violations(package: str, banned: tuple[str, ...]) -> list[str]:
    problems: list[str] = []
    for path in _files(package):
        for module, line, _ in _imports(path):
            if any(module == name or module.startswith(name + ".") for name in banned):
                problems.append(f"{path.relative_to(SRC)}:{line} imports {module}")
    return problems


def test_d9_d10_d11_layering() -> None:
    for package in sorted(FORBIDDEN):
        assert not _violations(package, FORBIDDEN[package]), package


def test_d13_imports_are_at_module_top() -> None:
    nested = [
        f"{path.relative_to(SRC)}:{line} imports {module}"
        for path in sorted(SRC.rglob("*.py"))
        for module, line, top in _imports(path)
        if not top
    ]
    assert not nested


def test_every_package_exists_and_names_its_contract() -> None:
    for package in EXPECTED_PACKAGES:
        init = SRC / package / "__init__.py"
        assert init.exists(), package
        docstring = ast.get_docstring(ast.parse(init.read_text(encoding="utf-8"))) or ""
        assert "docs/" in docstring, f"{package} does not name its contract"


def test_every_module_has_a_docstring() -> None:
    missing = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) is None
    ]
    assert not missing


def test_no_semicolon_joined_statements() -> None:
    joined: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for parent in ast.walk(tree):
            body = getattr(parent, "body", None)
            if not isinstance(body, list):
                continue
            lines = [node.lineno for node in body if isinstance(node, ast.stmt)]
            if len(lines) != len(set(lines)):
                joined.append(str(path.relative_to(SRC)))
    assert not joined
