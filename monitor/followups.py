from .configuration import resolve
from .configuration import zone
"""Independent no-reply reminders. Review timestamps never restart this clock."""

from datetime import datetime, timedelta
from .model import OPEN, now, stamp

MODES = {"reminder", "review", "blocked", "quiet", "handoff"}


def due_at(action):
    policy = action.get("follow_up") or {}
    if policy.get("mode") not in {"reminder", "review"}:
        return None
    if policy.get("due_at"):
        return stamp(policy["due_at"])
    base = datetime.fromisoformat(stamp(policy["anchor_at"])).astimezone(zone())
    remaining = policy.get('interval_days', resolve().get('reviews.followup_days'))
    while remaining:
        base += timedelta(days=1)
        if policy.get("basis", "calendar") == "calendar" or base.weekday() < 5:
            remaining -= 1
    due = stamp(base.isoformat())
    # A stated response/payment promise or deadline takes precedence over the
    # generic cadence. An explicit policy due date can document a different
    # context-specific handoff (for example the next working morning).
    return min([due] + [stamp(action[k]) for k in ("promised_at", "deadline") if action.get(k)])


def reminder_due(action, at=None):
    if action["status"] not in OPEN or (action.get("follow_up") or {}).get("mode") != "reminder":
        return False
    at = stamp(at or now())
    if action.get("snooze_until") and stamp(action["snooze_until"]) > at and not action.get("snooze_override"):
        return False
    due = due_at(action)
    return bool(due and due <= at)


def prepare(h, action, prior, evidence_ids, at=None):
    action.pop('custom_action_state',None)
    if prior and 'custom_action' in prior and 'custom_action' not in action:action['custom_action']=dict(prior['custom_action'])
    if action.get('custom_action'):
        ref=action['custom_action']
        if not isinstance(ref,dict) or set(ref)-{'bundle','action','parameters','approval_id'} or not all(isinstance(ref.get(k),str) and ref[k] for k in ('bundle','action')) or not isinstance(ref.get('parameters',{}),dict):raise ValueError('Invalid custom action reference')
    if not action.get("follow_up") and prior and prior.get("follow_up"):
        # A caller replacing case state must not silently discard a timer.
        action["follow_up"] = dict(prior["follow_up"])
    if action["status"] not in OPEN:
        return
    if not action.get("follow_up"):
        mode = {"waiting": "reminder", "watch": "review", "needs-user": "handoff"}[action["status"]]
        action["follow_up"] = {
            "mode": mode,
            'interval_days': resolve().get('reviews.followup_days'),
            "basis": "calendar",
            "anchor_at": stamp(action.get("first_recorded_at") or at or now()),
            "anchor_kind": "tracking-start; replace with verified communication time when known",
            "reason": "Configured default; adjust for promises, urgency, dependencies and user decisions when recording substantive evidence.",
            "evidence_ids": evidence_ids,
        }
    policy = action["follow_up"]
    if not isinstance(policy, dict) or policy.get("mode") not in MODES:
        raise ValueError("follow_up requires reminder/review/blocked/quiet/handoff mode")
    policy.setdefault('interval_days', resolve().get('reviews.followup_days'))
    policy.setdefault("basis", "calendar")
    days = policy["interval_days"]
    if type(days) is not int or not 1 <= days <= 90:
        raise ValueError("Follow-up interval must be 1..90 days")
    if policy["basis"] not in {"calendar", "working"}:
        raise ValueError("Follow-up basis must be calendar or working")
    h.limited(policy, "reason", 300)
    if not policy.get("evidence_ids"):
        raise ValueError("Follow-up policy needs evidence")
    h.evidence_exists(policy["evidence_ids"])
    if policy.get("anchor_at"):
        policy["anchor_at"] = stamp(policy["anchor_at"])
    if policy.get("due_at"):
        policy["due_at"] = stamp(policy["due_at"])
    if policy["mode"] in {"reminder", "review"} and not (policy.get("anchor_at") or policy.get("due_at")):
        raise ValueError("Active follow-up requires an anchor or explicit due date")
    if policy["mode"] == "blocked":
        dependencies = policy.get("depends_on")
        if not isinstance(dependencies, list) or not dependencies or any(not isinstance(x, str) or not x or x == action["id"] for x in dependencies):
            raise ValueError("Blocked follow-up requires named prerequisite action IDs")
