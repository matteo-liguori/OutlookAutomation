"""Authoritative source identities and collection boundary checks."""

from datetime import datetime, timedelta
from .model import stamp
from .profile import PROFILE

MAIL_STORES = PROFILE["mail"]


def validate_mail(source, pages, started_at, freshness):
    expected = MAIL_STORES[source]
    first = pages[0]
    if first.get("Store") != expected or not first.get("StoreId"):
        raise ValueError("Mail collection does not match the configured source store")
    if not first.get("SnapshotId") or not first.get("StartedAt"):
        raise ValueError("Collection requires the saved snapshot identity and capture time")
    if stamp(first["StartedAt"]) != stamp(started_at):
        raise ValueError("Ingestion start time differs from the actual snapshot capture")
    since, until = stamp(first["Since"]), stamp(first["Until"])
    if since > until:
        raise ValueError("Invalid mail window")
    for page in pages:
        if any(
            page.get(key) != first.get(key)
            for key in ("Store", "StoreId", "Since", "Until", "SnapshotId", "StartedAt")
        ):
            raise ValueError("Collector pages have different source/window/snapshot")
        for item in page["Items"]:
            if item.get("StoreId") != first["StoreId"]:
                raise ValueError("Message store identity does not match its collection")
            if not since <= stamp(item["Received"]) <= until:
                raise ValueError("Collected message is outside the requested window")
    if not freshness:
        return False
    if freshness.get("Store") != expected or freshness.get("LatestStoreId") != first["StoreId"]:
        raise ValueError("Freshness does not match the configured source store")
    if freshness.get("Status") != "current" or freshness.get("Current") is not True:
        return False
    checked = freshness.get("CheckedAt")
    if not checked or datetime.fromisoformat(stamp(checked)) < datetime.fromisoformat(
        stamp(started_at)
    ) - timedelta(minutes=15):
        return False
    newest = stamp(freshness.get("LatestReceived"))
    checked_at = datetime.fromisoformat(stamp(checked))
    return bool(
        newest
        and checked_at - timedelta(days=1)
        <= datetime.fromisoformat(newest)
        <= checked_at + timedelta(minutes=5)
    )
