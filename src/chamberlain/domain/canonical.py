"""Canonical JSON and content hashes.

Contract: docs/contracts/01-domain.md (D-14).
"""

import hashlib
import json

from pydantic import BaseModel


def canonical_json(model: BaseModel) -> str:
    """Sorted keys, no insignificant whitespace, `None` fields omitted."""
    data = model.model_dump(mode="json", exclude_none=True)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(model: BaseModel) -> str:
    """Lowercase hex SHA-256 of the canonical form."""
    return sha256_text(canonical_json(model))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
