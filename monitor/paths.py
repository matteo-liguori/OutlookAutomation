"""One private data location shared by the portable harness and platform helpers."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def data_root(root=ROOT):
    root = Path(root).resolve()
    pointer = root / '.local/data-root.txt'
    configured = os.environ.get('OUTLOOK_AUTOMATION_DATA')
    if not configured and pointer.is_file():
        configured = pointer.read_text(encoding='utf-8-sig').strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError('The private data directory must be an absolute path')
        return path.resolve()
    return root / '.local'


def configure_data(path, root=ROOT):
    root = Path(root).resolve()
    path = Path(path).expanduser().resolve()
    if os.environ.get('OUTLOOK_AUTOMATION_DATA') and data_root(root) != path:
        raise ValueError('Clear the data-directory environment override before configuring another location')
    if path == root or root.is_relative_to(path):
        raise ValueError('The data directory must not contain the source checkout')
    if path.is_relative_to(root) and path != root/'.local':
        raise ValueError('Use an external data directory or the ignored .local directory')
    pointer = root / '.local/data-root.txt'
    if pointer.exists() and data_root(root) != path:
        raise ValueError('Existing data location preserved; use manage_data.py to relocate it')
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(str(path)+'\n', encoding='utf-8')
    return path
