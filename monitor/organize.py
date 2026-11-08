"""Atomic, evidence-backed case organization without resolving obligations."""

import json
from .model import OPEN, digest, dumps, now


def organize(h, operation, payload):
    if operation not in {"merge", "split"}:
        raise ValueError("Unknown organization operation")
    key = payload["idempotency_key"]
    fingerprint = digest([operation, payload])
    with h.write():
        retry = h.db.execute("SELECT hash,result FROM mutations WHERE key=?", (key,)).fetchone()
        if retry:
            if retry[0] != fingerprint:
                raise ValueError("Idempotency key reused for different organization")
            return json.loads(retry[1])
        h.limited(payload, "reason", 600)
        h.limited(payload, "target_id", 180)
        evidence = payload.get("evidence_ids", [])
        if not evidence:
            raise ValueError("Organization needs evidence")
        h.evidence_exists(evidence)
        if operation == "merge":
            target_id = payload["target_id"]
            sources = payload["source_ids"]
            if not sources or len(sources) != len(set(sources)) or target_id in sources:
                raise ValueError("Merge needs distinct source cases, excluding target")
            ids = [target_id] + sources
        else:
            sources = [payload["source_id"]]
            target_id = payload["target_id"]
            ids = sources
            if (
                target_id in sources
                or h.db.execute("SELECT 1 FROM cases WHERE id=?", (target_id,)).fetchone()
            ):
                raise ValueError("Split target must be a new case ID")
            if not payload.get("action_ids") or len(set(payload["action_ids"])) != len(
                payload["action_ids"]
            ):
                raise ValueError("Split needs distinct action IDs")
        before = {cid: h.case(cid) for cid in ids}
        for cid, c in before.items():
            if payload.get("expected_revisions", {}).get(cid) != c["revision"]:
                raise ValueError("Stale or missing expected revision: " + cid)
            if c["status"] != "open":
                raise ValueError("Organization requires open cases: " + cid)
        after = json.loads(dumps(before))
        if operation == "merge":
            target = after[target_id]
            moved = []
            for sid in sources:
                source = after[sid]
                moved.extend(a["id"] for a in source["actions"])
                target["actions"].extend(source["actions"])
                source["actions"] = []
                target["decisions"].extend(source["decisions"])
                source["decisions"] = []
                target["evidence_ids"] = list(
                    dict.fromkeys(target.get("evidence_ids", []) + source.get("evidence_ids", []))
                )
                target["aliases"] = list(
                    dict.fromkeys(
                        target.get("aliases", [])
                        + source.get("aliases", [])
                        + [sid, source["title"]]
                    )
                )
                for name, values in source.get("match_keys", {}).items():
                    target.setdefault("match_keys", {})[name] = list(
                        dict.fromkeys(target.get("match_keys", {}).get(name, []) + values)
                    )
                source.update(status="closed", merged_into=target_id)
                source["related_cases"] = list(
                    dict.fromkeys(source.get("related_cases", []) + [target_id])
                )
            target["related_cases"] = list(dict.fromkeys(target.get("related_cases", []) + sources))
            for name in ["title", "position"]:
                if name in payload.get("target", {}):
                    target[name] = payload["target"][name]
        else:
            source = after[sources[0]]
            moving = set(payload["action_ids"])
            if not moving.issubset({a["id"] for a in source["actions"]}):
                raise ValueError("Split action does not belong to source")
            target = json.loads(dumps(source))
            target.update(
                id=target_id,
                revision=0,
                title=payload["target"]["title"],
                position=payload["target"]["position"],
                split_from=source["id"],
                status="open",
                related_cases=[source["id"]],
            )
            target.pop("merged_into", None)
            target["created_at"] = now()
            target["actions"] = [a for a in source["actions"] if a["id"] in moving]
            target.pop("split_into", None)
            if not any(a["status"] in OPEN for a in target["actions"]):
                target["status"] = "closed"
            source["actions"] = [a for a in source["actions"] if a["id"] not in moving]
            target["decisions"] = [
                dict(d, id=target_id + "::" + d["id"], inherited_from=d["id"])
                for d in source["decisions"]
            ]
            target["aliases"] = payload["target"].get("aliases", [])
            if "match_keys" in payload["target"]:
                target["match_keys"] = payload["target"]["match_keys"]
            source["related_cases"] = list(
                dict.fromkeys(source.get("related_cases", []) + [target_id])
            )
            source["split_into"] = list(dict.fromkeys(source.get("split_into", []) + [target_id]))
            if "source_position" in payload:
                source["position"] = payload["source_position"]
            if not any(a["status"] in OPEN for a in source["actions"]):
                source["status"] = "closed"
            after[target_id] = target
            moved = list(payload["action_ids"])
        # All mutations below share the same transaction. Failures roll back
        # ownership changes, case metadata, decision copies and audit together.
        at = now()
        original_actions = sorted(a["id"] for c in before.values() for a in c["actions"])
        final_actions = sorted(a["id"] for c in after.values() for a in c["actions"])
        if original_actions != final_actions:
            raise ValueError("Organization would lose or duplicate an action")
        for cid, c in after.items():
            h.limited(c, "title", 180)
            h.limited(c, "position", 1200)
            c["evidence_ids"] = list(dict.fromkeys(c.get("evidence_ids", []) + evidence))
            c["revision"] += 1
            c["updated_at"] = at
            h._save_case(c, cid, c["revision"], at)
        for cid in after:
            h.db.execute("DELETE FROM decisions WHERE case_id=?", (cid,))
        for cid, c in after.items():
            for a in c["actions"]:
                h.db.execute("UPDATE actions SET case_id=?,details=? WHERE id=?", (cid, dumps(a), a["id"]))
            for d in c["decisions"]:
                h.db.execute("INSERT INTO decisions VALUES(?,?,?)", (d["id"], cid, dumps(d)))
        result = {
            "operation": operation,
            "target_id": target_id,
            "source_ids": sources,
            "moved_action_ids": moved,
            "revisions": {cid: c["revision"] for cid, c in after.items()},
            "sending_approvals_transferred": False,
        }
        h.db.execute("INSERT INTO mutations VALUES(?,?,?)", (key, fingerprint, dumps(result)))
        return result
