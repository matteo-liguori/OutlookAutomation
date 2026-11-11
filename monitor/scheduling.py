from .configuration import resolve
from .configuration import zone
"""Explicit action review policy; never completes work or authorizes external actions."""

from datetime import datetime, timedelta, timezone
import json
from .model import digest, now, stamp


def validate(h, policy):
    if not isinstance(policy, dict):
        raise ValueError("review_policy must be an object")
    if policy.get("urgency") not in {"immediate", "high", "normal", "low"}:
        raise ValueError("review_policy requires immediate/high/normal/low urgency")
    days = policy.get("interval_days")
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 90:
        raise ValueError("Review interval must be 1..90 days")
    if policy.get("basis") not in {"calendar", "working"}:
        raise ValueError("Review basis must be calendar or working")
    if policy["urgency"] in {"immediate", "high"} and days > resolve().get('reviews.urgent_max_days'):
        raise ValueError("Immediate/high urgency cannot have more than three days between reviews")
    h.limited(policy, "reason", 300)
    if not policy.get("evidence_ids"):
        raise ValueError("Review policy needs evidence")
    h.evidence_exists(policy["evidence_ids"])


def cadence(action, at=None):
    policy = action["review_policy"]
    base = datetime.fromisoformat(
        stamp(action.get("reviewed_at") or action.get("first_recorded_at") or at or now())
    ).astimezone(zone())
    remaining = policy["interval_days"]
    while remaining:
        base += timedelta(days=1)
        if policy["basis"] == "calendar" or base.weekday() < 5:
            remaining -= 1
    return stamp(base.replace(hour=resolve().get('reviews.hour'), minute=0, second=0, microsecond=0).isoformat())


def review(h, payload):
    request_hash = digest(payload)
    retry = h.db.execute(
        "SELECT result FROM mutations WHERE key=?", (payload["idempotency_key"],)
    ).fetchone()
    if retry:
        result = json.loads(retry[0])
        if result.get("request_hash") != request_hash:
            raise ValueError("Idempotency key reused for different review")
        return result
    row = h.db.execute("SELECT case_id FROM actions WHERE id=?", (payload["action_id"],)).fetchone()
    if not row:
        raise ValueError("Unknown action")
    c = h.case(row[0])
    h.evidence_exists(payload.get("evidence_ids", []))
    if not payload.get("evidence_ids") or payload.get("review_kind") not in {
        "records",
        "mail",
        "source-check",
    }:
        raise ValueError("Review needs evidence and kind: records, mail or source-check")
    h.limited(payload, "outcome", 600)
    reviewed = stamp(payload.get("reviewed_at") or now())
    if reviewed > stamp(now()):
        raise ValueError("Cannot record a future review")
    a = next(a for a in c["actions"] if a["id"] == payload["action_id"])
    if a["status"] not in {"waiting", "watch", "needs-user"}:
        raise ValueError("Cannot review a resolved action")
    validate(h, payload["review_policy"])
    a.update(
        reviewed_at=reviewed,
        review_outcome=payload["outcome"],
        review_kind=payload["review_kind"],
        review_evidence_ids=payload["evidence_ids"],
        review_policy=payload["review_policy"],
    )
    case = {
        k: v
        for k, v in c.items()
        if k not in {"id", "revision", "updated_at", "actions", "decisions", "evidence_ids"}
    }
    case['card_reviewed_at'] = reviewed
    return h.update(
        dict(
            id=c["id"],
            expected_revision=payload["expected_revision"],
            idempotency_key=payload["idempotency_key"],
            evidence_ids=list(dict.fromkeys(c["evidence_ids"] + payload["evidence_ids"])),
            case=case,
            actions=c["actions"],
            decisions=c["decisions"],
            reason="Action review: " + payload["review_kind"],
            request_hash=request_hash,
        )
    )


def next_review(action, at=None):
    due, rule = _policy_next_review(action, at)
    from .followups import due_at

    chase = due_at(action) if (action.get("follow_up") or {}).get("mode") == "reminder" else None
    if chase and action["status"] in {"waiting", "watch"} and rule != "user-snooze" and chase < due:
        return chase, "no-reply-follow-up"
    return due, rule


def _policy_next_review(action, at=None):
    legacy, rule = _default_next_review(action, at)
    if not action.get("review_policy") or rule == "user-snooze" or action["status"] == "needs-user":
        return legacy, rule
    candidates = [(cadence(action, at), "action-policy")]
    reviewed = stamp(action.get("reviewed_at"))
    if rule not in {"weekly", "working-days"}:
        # Reviewed past deadlines remain facts but do not cause every-pass churn.
        if rule != "deadline" or not reviewed or reviewed < stamp(action["deadline"]):
            candidates.append((legacy, rule))
    if action.get("deadline"):
        deadline_action = dict(action)
        deadline_action.pop("explicit_review", None)
        deadline_action.pop("promised_at", None)
        due, deadline_rule = _default_next_review(deadline_action, at)
        if not reviewed or reviewed < stamp(action["deadline"]):
            candidates.append((due, deadline_rule))
    return min(candidates, key=lambda x: x[0])


def _default_next_review(action, at=None):
    if action.get("snooze_until") and (
        not action.get("reviewed_at")
        or stamp(action["reviewed_at"]) < stamp(action["snooze_until"])
    ):
        return stamp(action["snooze_until"]), "user-snooze"
    if action.get("explicit_review") and (
        not action.get("reviewed_at")
        or stamp(action["reviewed_at"]) < stamp(action["explicit_review"])
    ):
        return stamp(action["explicit_review"]), "explicit-date"
    if action.get("promised_at") and (
        not action.get("reviewed_at") or stamp(action["reviewed_at"]) < stamp(action["promised_at"])
    ):
        promised = datetime.fromisoformat(stamp(action["promised_at"])).astimezone(zone())
        for offset in range(3):
            day = promised + timedelta(days=offset)
            for hour in (9, 14, 19):
                slot = day.replace(hour=hour, minute=0, second=0, microsecond=0)
                if slot > promised:
                    return stamp(slot.isoformat()), "promised-date"
    if action.get("deadline"):
        deadline = datetime.fromisoformat(stamp(action["deadline"])).astimezone(zone())
        reviewed = datetime.fromisoformat(stamp(action.get("reviewed_at") or "1970-01-01"))
        milestones = [deadline - timedelta(days=n) for n in (7, 2, 0)]
        # Preserve the first missed checkpoint until an actual review records it.
        eligible = [d for d in milestones if d.astimezone(timezone.utc) > reviewed]
        due = eligible[0] if eligible else deadline
        first = action.get("first_recorded_at")
        if first and due < datetime.fromisoformat(stamp(first)) and not action.get("reviewed_at"):
            return stamp(first), "initial-deadline-review"
        return stamp(due.isoformat()), "deadline"
    days = 7
    base = datetime.fromisoformat(stamp(action.get("reviewed_at") or action.get("first_recorded_at") or at or now())).astimezone(
        zone()
    )
    if action["status"] == "needs-user":
        return stamp(base.isoformat()), "each-handoff"
    while days:
        base += timedelta(days=1)
        days -= 1
    return stamp(base.replace(hour=resolve().get('reviews.hour'), minute=0, second=0, microsecond=0).isoformat()), "weekly"
