"""One identity and review signature for every message ingestion path."""

import json
from .model import digest, dumps

REVIEW_FIELDS = (
    "disposition",
    "review_reason",
    "previous_review",
    "body_evidence_id",
    "case_id",
    "case_revision",
    "no_update_reason",
    "reviewed_run",
)
CONTENT_FIELDS = (
    "subject",
    "received",
    "message_class",
    "sender",
    "conversation_id",
    "internet_message_id",
)
METADATA_FIELDS = ("source", "entry_id", "store_id") + CONTENT_FIELDS


def encode(h, message):
    """Separate immutable metadata from each pass's review; preserve missing keys."""
    data = dict(message)
    metadata = {key: data.pop(key) for key in METADATA_FIELDS if key in data}
    version = digest(metadata)
    h.db.execute("INSERT OR IGNORE INTO message_versions VALUES(?,?)", (version, dumps(metadata)))
    # Source remains directly queryable without loading metadata.
    if "source" in metadata:
        data["source"] = metadata["source"]
    data["message_version"] = version
    return dumps(data)


def decode(h, raw):
    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    version = data.pop("message_version", None)
    if not version:
        return data
    row = h.db.execute("SELECT details FROM message_versions WHERE id=?", (version,)).fetchone()
    if not row:
        raise ValueError("Missing message metadata version: " + version)
    metadata = json.loads(row[0])
    if digest(metadata) != version:
        raise ValueError("Corrupt message metadata version: " + version)
    return dict(metadata, **data)


def identity(message):
    locator = message.get("internet_message_id") or (
        message.get("store_id", "") + ":" + message["entry_id"]
    )
    return digest([message["source"], locator])


def changed(previous, incoming):
    return any(key in incoming and previous.get(key) != incoming[key] for key in CONTENT_FIELDS)
