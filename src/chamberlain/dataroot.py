"""The data root must never sit inside the code checkout.

Private mail, calendar data and tokens live in the data root. If it were inside the folder that
holds the code, or inside a Git work tree that holds the code, one careless `git add .` could
publish it. Every entry point that opens the data root calls `refuse_inside_checkout` first.
"""

import os
from pathlib import Path

from chamberlain.schema.errors import ChamberlainError, ErrorCode


def code_folders() -> tuple[Path, ...]:
    """The folder holding the package, and the nearest Git work tree around it (if any)."""
    package = Path(__file__).resolve().parent
    folders = [package.parent]
    for parent in (package, *package.parents):
        if (parent / ".git").exists():
            folders.append(parent)
            break
    return tuple(folders)


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
