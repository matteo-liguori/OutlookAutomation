"""Canonical JSON and content hashes.

Contract: docs/contracts/01-domain.md (D-14).
"""

import hashlib
import json
from typing import Any, cast

from pydantic import BaseModel
from pydantic_core import to_jsonable_python


def _ordered(value: object) -> object:
    """Sets become sorted lists, so equal sets always give the same text."""
    if isinstance(value, dict):
        return {k: _ordered(v) for k, v in cast(dict[Any, object], value).items()}
    if isinstance(value, list | tuple):
        return [_ordered(v) for v in cast(list[object] | tuple[object, ...], value)]
    if isinstance(value, set | frozenset):
        members = cast(set[object] | frozenset[object], value)
        items = [to_jsonable_python(_ordered(v)) for v in members]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    return value


def canonical_json(model: BaseModel) -> str:
    """Sorted keys, sets in sorted order, no insignificant whitespace, `None` fields omitted."""
    data = to_jsonable_python(_ordered(model.model_dump(mode="python", exclude_none=True)))
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(model: BaseModel) -> str:
    """Lowercase hex SHA-256 of the canonical form."""
    return sha256_text(canonical_json(model))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
