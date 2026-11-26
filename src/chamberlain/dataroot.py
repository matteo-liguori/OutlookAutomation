"""The data root must never sit inside the code checkout.

Private mail, calendar data and tokens live in the data root. If it were inside the folder that
holds the code, or inside a Git work tree that holds the code, one careless `git add .` could
publish it. Every entry point that opens the data root calls `refuse_inside_checkout` first.
"""

import os
from pathlib import Path

from chamberlain.schema.errors import ChamberlainError, ErrorCode


def checkout_root() -> Path | None:
    """The nearest Git work tree around this package, when it runs from a source checkout."""
    return next((p for p in Path(__file__).resolve().parents if (p / ".git").exists()), None)


def code_folders() -> tuple[Path, ...]:
    """The folder holding the package, and the nearest Git work tree around it (if any)."""
    root = checkout_root()
    return (Path(__file__).resolve().parent.parent, *((root,) if root else ()))


def _inside(path: Path, folder: Path) -> bool:
    child = os.path.normcase(os.path.realpath(path))
    top = os.path.normcase(os.path.realpath(folder))
    try:
        return os.path.commonpath([child, top]) == top
    except ValueError:  # another drive
        return False


def refuse_inside_checkout(
    data_root: Path | str, *, folders: tuple[Path, ...] | None = None
) -> Path:
    """Return the data root, or raise `config.invalid` when it is inside the code checkout."""
    path = Path(data_root)
    for folder in code_folders() if folders is None else folders:
        if _inside(path, folder):
            raise ChamberlainError(
                ErrorCode.CONFIG_INVALID,
                f"The data folder {path} is inside the code folder {folder}. Private mail and "
                "calendar data could be committed to Git by accident.",
                fix="Choose a data folder outside the code checkout, for example a folder "
                "next to it, and pass it with --data or CHAMBERLAIN_DATA.",
                field="data",
            )
    return path


def data_root_arg(data: str | None) -> Path:
    """The `--data` value or `CHAMBERLAIN_DATA`, refused when inside the checkout."""
    root = data or os.environ.get("CHAMBERLAIN_DATA")
    if not root:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            "Give the data root with --data or CHAMBERLAIN_DATA.",
            field="data",
        )
    return refuse_inside_checkout(root)
