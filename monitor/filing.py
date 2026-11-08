"""Current filing checkpoints, independent of mail review/report watermarks."""

import json
from datetime import datetime, timedelta
from .model import MAIL, dumps, now, stamp

KEY = "filing:"


def enabled(h):
    return (
        h.db.execute("SELECT 1 FROM meta WHERE key='filing_enabled' AND value='1'").fetchone()
        is not None
    )


def status(h, at=None):
    at = stamp(at or now())
    floor = stamp((datetime.fromisoformat(at) - timedelta(days=7)).isoformat())
    rows = []
    for source in MAIL:
        r = h.db.execute("SELECT value FROM meta WHERE key=?", (KEY + source,)).fetchone()
        data = json.loads(r[0]) if r else {}
        success = data.get("last_success") or {}
        through = success.get("until")
        rows.append(
            {
                "type": "filing_checkpoint",
                "source": source,
                "last_successful_at": success.get("completed_at"),
                "filed_through": through,
                "since": max(floor, through or floor),
                "until": at,
                "maximum_lookback_days": 7,
                "older_gap_outside_scope": bool(through and through < floor),
                "last_attempt_status": (data.get("last_attempt") or {}).get("status"),
                "last_success_evidence_ids": success.get("evidence_ids", []),
            }
        )
    return rows


def record(h, payload):
    p = dict(payload)
    if p.get("source") not in MAIL:
        raise ValueError("Unknown filing source")
    if p.get("status") not in {"complete", "partial", "failed"}:
        raise ValueError("Filing status must be complete, partial or failed")
    for field in ("since", "until", "completed_at"):
        if not p.get(field):
            raise ValueError("Filing requires " + field)
        p[field] = stamp(p[field])
    floor = stamp((datetime.fromisoformat(p["until"]) - timedelta(days=7)).isoformat())
    if not floor <= p["since"] <= p["until"] <= p["completed_at"] <= stamp(now()):
        raise ValueError("Filing window must be at most seven days, ordered and not in the future")
    for field in ("reviewed_count", "moved_count", "verified_move_count", "failed_count"):
        if type(p.get(field)) is not int or p[field] < 0:
            raise ValueError("Filing requires nonnegative integer " + field)
    if p["verified_move_count"] > p["moved_count"] or p["moved_count"] > p["reviewed_count"]:
        raise ValueError("Invalid filing counts")
    if not p.get("evidence_ids") or p.get("provenance") not in {"command", "assistant"}:
        raise ValueError("Filing requires evidence and provenance")
    if p["status"] == "complete" and (
        not p.get("complete")
        or p["failed_count"]
        or p["verified_move_count"] != p["moved_count"]
        or p.get("unread_preserved") is not True
    ):
        raise ValueError(
            "Successful filing requires complete review and verified moves with unchanged unread state"
        )
    with h.write():
        h.evidence_exists(p["evidence_ids"])
        r = h.db.execute("SELECT value FROM meta WHERE key=?", (KEY + p["source"],)).fetchone()
        data = json.loads(r[0]) if r else {}
        old = data.get("last_success") or {}
        attempt = data.get("last_attempt") or {}
        if attempt and p["completed_at"] < attempt["completed_at"]:
            raise ValueError("Filing attempt predates the current record")
        if old and p["until"] < old["until"]:
            raise ValueError("Filing checkpoint cannot regress")
        if p == attempt:
            return {"source": p["source"], "status": p["status"], "unchanged": True}
        if p["status"] == "complete":
            if old and p["since"] > max(floor, old["until"]):
                raise ValueError("Filing skipped part of the required catch-up window")
            data["last_success"] = p
        data["last_attempt"] = p
        h.db.execute(
            "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (KEY + p["source"], dumps(data)),
        )
        h.db.execute(
            "INSERT INTO meta VALUES('filing_enabled','1') ON CONFLICT(key) DO UPDATE SET value='1'"
        )
    return {
        "source": p["source"],
        "status": p["status"],
        "checkpoint_advanced": p["status"] == "complete",
    }
