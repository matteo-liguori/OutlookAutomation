"""Import completed collector pages; preserve incompleteness instead of guessing."""

import json
from .model import MAIL, dumps, stamp
from .messages import identity, changed, REVIEW_FIELDS
from .sources import validate_mail


def import_pages(h, rid, source, pages, started_at, freshness=None):
    if source not in MAIL or not pages:
        raise ValueError("Expected an intended mail source and at least one page")
    fresh = validate_mail(source, pages, started_at, freshness)
    expected = 0
    first = pages[0]["FirstId"]
    rows = {}
    evidence_ids = []
    complete = False
    errors = []
    for index, page in enumerate(pages):
        if page["Offset"] != expected or page["FirstId"] != first:
            raise ValueError("Missing/reordered page or changed mailbox snapshot")
        if index and any(page[k] != pages[0][k] for k in ["StoreId", "Since", "Until"]):
            raise ValueError("Collector pages have different source/window")
        if complete:
            raise ValueError("Unexpected page after completed scan")
        errors.extend(page.get("Errors", []))
        complete = bool(page["Complete"])
        expected = page["NextOffset"]
        page_candidates = []
        for row in page["Items"]:
            candidate = {
                "kind": "candidate",
                "source": source,
                "entry_id": row["EntryId"],
                "store_id": row["StoreId"],
                "received": stamp(row["Received"]),
                "message_class": row["MessageClass"],
                "subject": row["Subject"],
                "sender": row["SenderEmail"],
                "conversation_id": row.get("ConversationId"),
                "internet_message_id": row.get("InternetMessageId"),
            }
            cid = identity(candidate)
            candidate["id"] = cid
            page_candidates.append(candidate)
        with h.write():
            index_rows = [
                {
                    "id": c["id"],
                    "message_version": json.loads(h.encode_message(c))["message_version"],
                }
                for c in page_candidates
            ]
        receipt = {key: value for key, value in page.items() if key != "Items"}
        receipt["Items"] = index_rows
        eid = h.put_evidence(
            {
                "source": source,
                "kind": "collector-index",
                "captured_at": stamp(started_at),
                "body": dumps(receipt),
            }
        )["id"]
        evidence_ids.append(eid)
        for candidate in page_candidates:
            cid = candidate["id"]
            candidate["evidence_ids"] = [eid]
            candidate["suggested_cases"] = h.suggest_cases(candidate)
            previous = h.db.execute(
                "SELECT details FROM message_reviews WHERE id=? AND source=?", (cid, source)
            ).fetchone()
            if previous:
                old = h.decode_message(previous[0])
                if not changed(old, candidate):
                    candidate.update({key: old[key] for key in REVIEW_FIELDS if key in old})
                    candidate.update(
                        disposition="duplicate",
                        review_reason="Previously reviewed identical message",
                        previous_review={"id": cid, "run_id": old.get("reviewed_run")},
                    )
                    candidate["evidence_ids"] = list(
                        dict.fromkeys(candidate["evidence_ids"] + old.get("evidence_ids", []))
                    )
            if cid in rows and changed(rows[cid], candidate):
                raise ValueError("Conflicting stable message IDs; manual reconciliation required")
            rows[cid] = candidate
    h.record_runs(rid, list(rows.values()))
    received = [c["received"] for c in rows.values()]
    freshness_ids = (
        [
            h.put_evidence(
                {
                    "source": source,
                    "kind": "freshness-probe",
                    "captured_at": stamp(started_at),
                    "body": dumps(freshness),
                    "provenance": "command",
                }
            )["id"]
        ]
        if freshness
        else []
    )
    result = {
        "kind": "source",
        "source": source,
        "status": "current" if complete and not errors and fresh else "unavailable",
        "complete": complete and not errors,
        "count": len(rows),
        "candidate_count": len(rows),
        "newest": max(received) if received else (stamp(freshness.get("LatestReceived")) if fresh else None),
        "since": pages[0]["Since"],
        "until": pages[0]["Until"],
        "started_at": stamp(started_at),
        "evidence_ids": evidence_ids,
        "freshness_evidence_ids": freshness_ids,
        "provenance": "command",
        "errors": errors,
    }
    h.record_run(rid, result)
    return result
