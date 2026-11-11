from .configuration import resolve
from .configuration import zone
"""Optional operational learning, excluded from current case briefings."""

import json
import re
from datetime import datetime, timedelta
from .model import dumps, now

STATUSES = {"proposed", "accepted", "deferred", "implemented", "rejected"}
REVIEW_KEY = "upgrades_review"


def review_prompt(h, at):
    """In afternoon briefings, check cadence without loading backlog contents."""
    if datetime.fromisoformat(at).astimezone(zone()).hour < resolve().get('reviews.upgrades_after_hour'):
        return []
    row = h.db.execute("SELECT value FROM meta WHERE key=?", (REVIEW_KEY,)).fetchone()
    state = json.loads(row[0]) if row else {}
    if state.get("next_due_at") and at < state["next_due_at"]:
        return []
    return [{"type": "upgrades_review_due", "last_reviewed_at": state.get("last_reviewed_at"),
             "instruction": "After this mail round, spend about a minute reviewing improvement list against concrete issues noticed during use. Add useful suggestions with improvement add --input - (items: id/observation/proposal/validation/references; evidence_ids); reuse existing entries. Keep the backlog solely here and implement on development requests. Finish with improvement checkpoint --input - (summary) to defer this prompt five days."}]


def checkpoint(h, payload):
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 600:
        raise ValueError("summary requires 1..600 characters describing the actual review")
    at = now()
    state = {"last_reviewed_at": at,
             "next_due_at": (datetime.fromisoformat(at) + timedelta(days=resolve().get('reviews.upgrades_days'))).isoformat(timespec="seconds"),
             "summary": summary, "evidence_ids": payload.get("evidence_ids", [])}
    with h.write():
        h.evidence_exists(state["evidence_ids"])
        h.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (REVIEW_KEY, dumps(state)))
    return state


def add(h, payload):
    """Add user-requested upgrades; optionally move an exact context record atomically."""
    entries = payload.get("items")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 3:
        raise ValueError("items requires one to three upgrades")
    ids = []
    for entry in entries:
        item_id = entry.get("id", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", item_id) or item_id in ids:
            raise ValueError("Invalid or duplicate upgrade id")
        ids.append(item_id)
        for field in ("observation", "proposal", "validation"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 600:
                raise ValueError(f"{field} requires 1..600 characters")
        refs = entry.get("references")
        if not isinstance(refs, list) or not 1 <= len(refs) <= 5 or any(
            not isinstance(r, str) or not r.strip() or len(r) > 200 for r in refs
        ):
            raise ValueError("references requires one to five locators")
    evidence_ids = payload.get("evidence_ids", [])
    if not evidence_ids:
        raise ValueError("User-requested upgrades require evidence")
    source = payload.get("source_context")
    with h.write():
        h.evidence_exists(evidence_ids)
        if source:
            row = h.db.execute("SELECT details FROM context WHERE id=?", (source["id"],)).fetchone()
            if not row or row[0] != source.get("expected_details"):
                raise ValueError("Source context changed or is missing; nothing moved")
        for entry in entries:
            if h.db.execute("SELECT 1 FROM improvements WHERE id=?", (entry["id"],)).fetchone():
                raise ValueError("Upgrade already exists; review the existing item")
            details = dict(entry, evidence_ids=evidence_ids, origin="user-request",
                           occurrences=1, last_seen=now())
            h.db.execute("INSERT INTO improvements VALUES(?,?,1,?)",
                         (entry["id"], "proposed", dumps(details)))
        if source:
            h.db.execute("DELETE FROM context WHERE id=?", (source["id"],))
    return {"added_ids": ids, "removed_context_id": source["id"] if source else None}


def reflect(h, run_id, payload):
    findings = payload.get("findings")
    if not isinstance(findings, list) or len(findings) > 3:
        raise ValueError("findings must be a list of zero to three observations")
    allowed = {"id", "observation", "proposal", "validation", "references"}
    for item in findings:
        if not isinstance(item, dict) or set(item) != allowed:
            raise ValueError(
                "Each finding requires id, observation, proposal, validation, references only"
            )
        if not isinstance(item["id"], str) or not re.fullmatch(
            "[a-z0-9][a-z0-9-]{0,79}", item["id"]
        ):
            raise ValueError("id must be a stable lowercase slug, at most 80 characters")
        for field in ("observation", "proposal", "validation"):
            if (
                not isinstance(item[field], str)
                or not item[field].strip()
                or len(item[field]) > 600
            ):
                raise ValueError(f"{field} requires 1..600 characters")
        refs = item["references"]
        if (
            not isinstance(refs, list)
            or not 1 <= len(refs) <= 5
            or any(not isinstance(r, str) or not r.strip() or len(r) > 200 for r in refs)
        ):
            raise ValueError("references requires 1..5 locators, each at most 200 characters")
    if len({i["id"] for i in findings}) != len(findings):
        raise ValueError("Duplicate finding id within reflection")
    canonical = {"findings": findings}
    with h.write():
        run = h.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run or run[0] not in ("complete", "partial"):
            raise ValueError("Reflection requires a finalized complete or partial run")
        previous = h.db.execute(
            "SELECT details FROM reflections WHERE run_id=?", (run_id,)
        ).fetchone()
        if previous:
            if json.loads(previous[0])["input"] != canonical:
                raise ValueError("Reflection already recorded; review existing suggestions instead")
            return {"run_id": run_id, "recorded": True, "duplicate": True}
        for item in findings:
            previous = h.db.execute(
                "SELECT details FROM improvements WHERE id=?", (item["id"],)
            ).fetchone()
            if previous:
                current = json.loads(previous[0])
                current["occurrences"] += 1
                current["last_run"] = run_id
                current["last_seen"] = now()
                # Keep the reviewed proposal and status; observations remain explicitly retrievable per run.
                h.db.execute(
                    "UPDATE improvements SET revision=revision+1,details=? WHERE id=?",
                    (dumps(current), item["id"]),
                )
            else:
                current = {
                    **item,
                    "first_run": run_id,
                    "last_run": run_id,
                    "occurrences": 1,
                    "last_seen": now(),
                }
                h.db.execute(
                    "INSERT INTO improvements VALUES(?,?,1,?)",
                    (item["id"], "proposed", dumps(current)),
                )
        h.db.execute(
            "INSERT INTO reflections VALUES(?,?)",
            (run_id, dumps({"at": now(), "input": canonical})),
        )
        h.touch("run", run_id)
    return {"run_id": run_id, "recorded": True, "suggestion_ids": [i["id"] for i in findings]}


def review(h, payload):
    if payload.get("status") not in STATUSES:
        raise ValueError("Invalid improvement status")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 600:
        raise ValueError("reason requires 1..600 characters; implemented must cite verification")
    with h.write():
        row = h.db.execute("SELECT * FROM improvements WHERE id=?", (payload["id"],)).fetchone()
        if not row or row["revision"] != payload.get("expected_revision"):
            raise ValueError("Unknown improvement or stale expected_revision")
        current = json.loads(row["details"])
        current["review"] = {"at": now(), "reason": reason}
        h.db.execute(
            "UPDATE improvements SET status=?,revision=revision+1,details=? WHERE id=?",
            (payload["status"], dumps(current), payload["id"]),
        )
    return {"id": payload["id"], "revision": row["revision"] + 1, "status": payload["status"]}


def items(h, status=None, item_id=None):
    rows = h.db.execute("SELECT * FROM improvements ORDER BY id").fetchall()
    return [
        {**json.loads(r["details"]), "status": r["status"], "revision": r["revision"]}
        for r in rows
        if (not status or r["status"] == status) and (not item_id or r["id"] == item_id)
    ]
