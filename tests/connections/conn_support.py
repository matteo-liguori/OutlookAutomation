"""Helpers for the connection and importer tests: synthetic profiles, an in-memory secret store."""

from pathlib import Path
from typing import Any

import yaml


class MemorySecrets:
    """A `SecretStore` that keeps everything in a dict (never the real credential store)."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get(self, service: str, name: str) -> str | None:
        return self.items.get((service, name))

    def set(self, service: str, name: str, value: str) -> None:
        self.items[(service, name)] = value

    def delete(self, service: str, name: str) -> bool:
        return self.items.pop((service, name), None) is not None


def full_profile() -> dict[str, Any]:
    """One source of every kind, with synthetic ids."""
    return {
        "version": 1,
        "timezone": "Europe/London",
        "write_policy": {"send_known": "human_confirmation", "sends_per_hour": 10},
        "sources": {
            "mail:work": {
                "kind": "thunderbird",
                "account_id": "account1",
                "mailbox": "me@example.test",
                "inbox_folder_id": "account1:/INBOX",
                "sent_folder_id": "account1:/Sent",
                "draft_folder_id": "account1:/Drafts",
                "trash_folder_id": "account1:/Trash",
            },
            "contacts:work": {"kind": "thunderbird_contacts", "address_book_ids": ["book1"]},
            "calendar:work": {"kind": "owl", "calendar_id": "owl-cal-1"},
            "calendar:primary": {
                "kind": "google",
                "account": "me@gmail.example",
                "calendar_id": "primary",
            },
            "calendar:icloud": {
                "kind": "caldav",
                "url": "https://caldav.example.test/me/home/",
                "username": "me@icloud.example",
                "credential": "password:me@icloud.example",
            },
        },
    }


def write_profile(data_root: Path, document: object) -> Path:
    path = data_root / "config" / "connections.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path
