from .configuration import resolve
"""Disposable, independently owned case caches and their automatic lifecycle."""

import json
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from .model import OPEN, digest, dumps, now, stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_messages(case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,message_key TEXT,metadata TEXT NOT NULL,body TEXT NOT NULL,cached_at TEXT NOT NULL,PRIMARY KEY(case_id,message_key));
CREATE TABLE IF NOT EXISTS thread_attachments(case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,sha256 TEXT,path TEXT NOT NULL,details TEXT NOT NULL,PRIMARY KEY(case_id,sha256));
CREATE TABLE IF NOT EXISTS thread_cleanup(case_id TEXT PRIMARY KEY,details TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mail_snapshots(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,details TEXT NOT NULL);
"""
BODY_KINDS = {"message-body", "full-message-body", "full-message", "email-body", "mail-body"}
LOCATOR_KEYS = {"EntryId", "StoreId", "InternetMessageId", "ConversationId", "Subject", "SenderEmail", "Received", "To", "Cc", "FolderPath", "MessageClass", "AttachmentNames", "Attachments", "entry_id", "store_id", "internet_message_id", "conversation_id", "subject", "sender", "received", "source"}


def initialize(h):
    h.db.executescript(SCHEMA)
    h.db.commit()


def startup(h):
    row = h.db.execute("SELECT value FROM meta WHERE key='thread_cache_maintenance'").fetchone()
    if row:
        previous = json.loads(row[0])
        overdue = h.db.execute("SELECT 1 FROM cases WHERE status='closed' AND json_extract(details,'$.expires_at')<=? LIMIT 1", (now(),)).fetchone()
        queued = h.db.execute('SELECT 1 FROM thread_cleanup LIMIT 1').fetchone()
        if previous.get("at", "")[:10] == now()[:10] and not previous.get("failures") and not overdue and not queued:
            return
    sweep(h)


def folder(h, cid):
    slug = re.sub(r"[^A-Za-z0-9_-]", "_", cid)[:60]
    reserved = {"CON", "PRN", "AUX", "NUL", *("COM"+str(i) for i in range(1,10)), *("LPT"+str(i) for i in range(1,10))}
    # Uppercase IDs retain their readable path; escaped IDs cannot collide on Windows.
    name = cid if cid == slug and cid == cid.upper() and cid not in reserved and cid else "~" + slug + "-" + digest(cid)[:16]
    return h.path.parent.resolve() / "threads" / name


def require_open(h, cid):
    c = h.case(cid)
    if c["status"] != "open":
        raise ValueError("Cannot cache assets for a closed thread")
    return c


def message_parts(message):
    if not isinstance(message, dict):
        raise ValueError("Message must be an object")
    body = message.get("Body", message.get("body"))
    if not isinstance(body, str):
        raise ValueError("Message requires a full text Body/body")
    metadata = {k: v for k, v in message.items() if k not in {"Body", "body", "standing_sending_permissions", "standing_sending_instruction"}}
    internet_id = message.get("InternetMessageId") or message.get("internet_message_id")
    locator = internet_id
    pair = [message.get("StoreId", message.get("store_id")), message.get("EntryId", message.get("entry_id"))]
    locator = locator or (pair if all(pair) else None) or message.get("cache_identity")
    if not locator or locator == [None, None]:
        raise ValueError("Message requires a server or store-qualified message identifier")
    return digest([None if internet_id else message.get("source", message.get("StoreId", message.get("store_id"))), locator]), metadata, body


def put_message(h, payload):
    cid, message = payload["case_id"], payload["message"]
    key, metadata, body = message_parts(message)
    with h.write():
        require_open(h, cid)
        old=h.db.execute('SELECT metadata FROM thread_messages WHERE case_id=? AND message_key=?',(cid,key)).fetchone()
        locators=json.loads(old[0]).get('CacheLocators',[]) if old else []
        locator=[metadata.get('StoreId',metadata.get('store_id')),metadata.get('EntryId',metadata.get('entry_id'))]
        if all(locator) and locator not in locators:locators.append(locator)
        metadata['CacheLocators']=locators
        h.db.execute("INSERT INTO thread_messages VALUES(?,?,?,?,?) ON CONFLICT(case_id,message_key) DO UPDATE SET metadata=excluded.metadata,body=excluded.body", (cid, key, dumps(metadata), body, now()))
    eid = h.put_evidence({"kind": "mail-source-reference", "source": metadata.get("source", "thread-cache"), "case_id": cid, "body": dumps({"case_id": cid, "message_key": key, "body_sha256": digest(body), "locator": {k: v for k, v in metadata.items() if k in LOCATOR_KEYS}})})["id"]
    metadata['CacheEvidenceId'] = eid
    with h.write():
        h.db.execute('UPDATE thread_messages SET metadata=? WHERE case_id=? AND message_key=?', (dumps(metadata),cid,key))
    from .approvals import sending_notice
    return {"case_id": cid, "message_key": key, "body_evidence_id": eid, **sending_notice(h, cid)}


def find_message(h, payload):
    require_open(h, payload["case_id"])
    for row in h.db.execute("SELECT * FROM thread_messages WHERE case_id=?", (payload["case_id"],)):
        m = json.loads(row["metadata"])
        if payload.get("message_key") == row["message_key"] or (
            payload.get("entry_id") and m.get("EntryId", m.get("entry_id")) == payload["entry_id"]
            and payload.get("store_id") and m.get("StoreId", m.get("store_id")) == payload["store_id"]
        ) or (payload.get('store_id') and payload.get('entry_id') and [payload['store_id'],payload['entry_id']] in m.get('CacheLocators',[])):
            if any(a.get('downloaded') and not Path(a.get('path','')).is_file() for a in m.get('CachedAttachments', [])):
                return {'found':False,'reason':'Owned attachment missing; retrieve correspondence again'}
            from .approvals import sending_notice
            return {"found": True, "message_key": row["message_key"], "message": dict(m, Body=row["body"], **sending_notice(h, payload['case_id']))}
    return {"found": False}


def attachment_path(h, cid, name, attachment_id):
    require_open(h, cid)
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(name.replace("\\", "/")).name)[:120].strip(" .") or "attachment"
    return folder(h, cid) / "attachments" / (digest(attachment_id)[:20] + "-" + name)


def register_attachment(h, payload):
    from .maintenance import file_hash
    cid = payload["case_id"]
    with h.write():
        require_open(h, cid)
        path = Path(payload["path"]).absolute()
        base = folder(h, cid) / "attachments"
        checked_path(h.path.parent, path)
        if not path.resolve().is_relative_to(base) or not path.is_file():
            raise ValueError("Attachment must be in its owning thread's attachment folder")
        sha = file_hash(path)
        old = h.db.execute("SELECT path FROM thread_attachments WHERE case_id=? AND sha256=?", (cid, sha)).fetchone()
        if old and Path(old[0]).is_file() and Path(old[0]).resolve() != path.resolve():
            path.unlink()
            path = Path(old[0])
        details = {k: v for k, v in payload.items() if k not in {"case_id", "path"}}
        details.update(bytes=path.stat().st_size)
        h.db.execute("INSERT INTO thread_attachments VALUES(?,?,?,?) ON CONFLICT(case_id,sha256) DO UPDATE SET path=excluded.path,details=excluded.details", (cid, sha, str(path), dumps(details)))
    return {"case_id": cid, "sha256": sha, "path": str(path)}


def checked_path(root, path):
    root, raw = Path(root).resolve(), Path(path).absolute()
    if raw == root or not raw.resolve().is_relative_to(root):
        raise ValueError("Thread cleanup escaped monitoring root")
    for node in (raw, *raw.parents):
        if node.is_symlink() or (hasattr(node, "is_junction") and node.is_junction()):
            raise ValueError("Thread cleanup refuses links/junctions")
        if node == root:
            break
    return raw


def lifecycle(h, case, prior=None, at=None):
    at = stamp(at or now())
    if case.get("status", "open") == "closed":
        closed = (prior or {}).get("closed_at") or case.get("closed_at") or at
        case["closed_at"] = closed
        case["expires_at"] = stamp((datetime.fromisoformat(closed) + timedelta(days=resolve().get('maintenance.closed_thread_days'))).isoformat())
        h.db.execute("INSERT OR IGNORE INTO thread_cleanup VALUES(?,?)", (case["id"], dumps({"queued_at": at})))
    else:
        case.pop("closed_at", None)
        case.pop("expires_at", None)
        h.db.execute("DELETE FROM thread_cleanup WHERE case_id=?", (case["id"],))
    if case.get("status", "open") == "closed" or (prior or {}).get("status") == "closed":
        h._thread_dirty = True


def pending_approval(details):
    state = str(details.get("status", "")).lower()
    return state not in {"sent", "verified-sent", "done", "revoked", "withdrawn", "replaced", "superseded", "dismissed", "cancelled", "completed"}


def mail_nodes(value):
    if isinstance(value, dict):
        if isinstance(value.get("Body"), str) and any(k in value for k in ("Subject", "EntryId", "SenderEmail")):
            yield value
        elif isinstance(value.get("body"), str) and any(k in value for k in ("subject", "entry_id", "internet_message_id")):
            yield value
        for k, child in value.items():
            if k not in {"Body", "body"}:
                yield from mail_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from mail_nodes(child)


def remove_case_refs(value, cid, action_ids):
    """Remove expired thread-specific fragments from receipts, never other cases' text."""
    if isinstance(value, dict):
        if value.get("case_id", value.get("case")) == cid or (isinstance(value.get('id'),str) and value['id'] in {cid, *action_ids}):
            return {"expired_thread": True}
        return {k: remove_case_refs(v, cid, action_ids) for k, v in value.items() if k != cid and k not in action_ids}
    if isinstance(value, list):
        return [remove_case_refs(v, cid, action_ids) for v in value if not (isinstance(v, str) and v in {cid, *action_ids})]
    return value


def expire_case(db, cid):
    action_ids = {r[0] for r in db.execute("SELECT id FROM actions WHERE case_id=?", (cid,))}
    for table in ("decisions", "approvals", "standing_approvals", "issues", "actions"):
        db.execute(f"DELETE FROM {table} WHERE case_id=?", (cid,))
    db.execute("DELETE FROM reported_cases WHERE case_id=?", (cid,))
    for table in ("thread_messages", "thread_attachments"):
        db.execute(f"DELETE FROM {table} WHERE case_id=?", (cid,))
    # Clear case-specific copies in historical run manifests and candidate ledgers.
    for table, key, col in (("runs", "id", "details"), ("mutations", "key", "result")):
        for r in db.execute(f"SELECT {key},{col} FROM {table}").fetchall():
            value = json.loads(r[col]); clean = remove_case_refs(value, cid, action_ids)
            if clean != value:
                if table == 'mutations' and clean == {'expired_thread': True}:
                    db.execute('DELETE FROM mutations WHERE key=?', (r[key],))
                else:
                    db.execute(f"UPDATE {table} SET {col}=? WHERE {key}=?", (dumps(clean), r[key]))
    for table in ("candidates", "message_reviews"):
        for r in db.execute(f"SELECT rowid,details FROM {table}").fetchall():
            if json.loads(r["details"]).get("case_id") == cid:
                db.execute(f"DELETE FROM {table} WHERE rowid=?", (r["rowid"],))
    for row in db.execute("SELECT id,metadata,body FROM evidence").fetchall():
        meta=json.loads(row['metadata'])
        try:
            body=json.loads(row['body'])
        except (ValueError,TypeError):
            body=None
        owner = meta.get('owner_case_id', meta.get('case_id'))
        if meta.get('scope') != 'standing' and (owner==cid or (owner is None and isinstance(body,dict) and body.get('case_id')==cid)):
            # Keep a reference-safe tombstone: another durable receipt may cite this ID.
            db.execute('UPDATE evidence SET metadata=?,body=? WHERE id=?', (dumps({'kind':'expired-thread-reference'}),dumps({'expired_thread':True}),row['id']))
    db.execute('DELETE FROM thread_cleanup WHERE case_id=?',(cid,))
    db.execute("DELETE FROM cases WHERE id=?", (cid,))


def sweep(h, at=None):
    if getattr(h,"_thread_sweeping",False):return {'already_running':True}
    at = stamp(at or now())
    result = {"closed_caches_deleted": 0, "expired_threads": 0, "failures": []}
    h._thread_sweeping = True
    try:
        with h.write():
            for c in h.all_cases():
                if c["status"] != "closed":
                    continue
                pending = [json.loads(r[0]) for r in h.db.execute("SELECT details FROM approvals WHERE case_id=?", (c["id"],))]
                if any(pending_approval(a) for a in pending):
                    result["failures"].append({"case_id": c["id"], "error": "Closed case has unresolved sending approval; preserved until reconciled"})
                    continue
                if not c.get("closed_at"):
                    # Older direct inserts and restored databases get a truthful starting point.
                    details = json.loads(h.db.execute("SELECT details FROM cases WHERE id=?", (c["id"],)).fetchone()[0])
                    details.update(closed_at=at, expires_at=stamp((datetime.fromisoformat(at)+timedelta(days=resolve().get('maintenance.closed_thread_days'))).isoformat()))
                    h.db.execute("UPDATE cases SET details=? WHERE id=?", (dumps(details), c["id"]))
                    c.update(details)
                changed = bool(h.db.execute('SELECT 1 FROM thread_messages WHERE case_id=? UNION SELECT 1 FROM thread_attachments WHERE case_id=? UNION SELECT 1 FROM thread_cleanup WHERE case_id=?',(c['id'],c['id'],c['id'])).fetchone())
                h.db.execute("DELETE FROM thread_messages WHERE case_id=?", (c["id"],))
                target = folder(h, c["id"])
                try:
                    if target.exists():
                        checked_path(h.path.parent / "threads", target)
                        for child in target.rglob("*"):
                            checked_path(h.path.parent / "threads", child)
                        shutil.rmtree(target)
                    h.db.execute("DELETE FROM thread_attachments WHERE case_id=?", (c["id"],))
                    h.db.execute("DELETE FROM thread_cleanup WHERE case_id=?", (c["id"],))
                    for approval in h.db.execute("SELECT id,details FROM approvals WHERE case_id=?",(c['id'],)).fetchall():
                        d=json.loads(approval['details'])
                        if not pending_approval(d) and isinstance(d.get('message'),dict) and 'body' in d['message']:
                            d['message']['body_sha256']=digest(d['message'].pop('body'))
                            h.db.execute('UPDATE approvals SET details=? WHERE id=?',(dumps(d),approval['id']))
                    result["closed_caches_deleted"] += int(changed)
                    if c.get("expires_at", at) <= at:
                        expire_case(h.db, c["id"])
                        result["expired_threads"] += 1
                except (OSError, ValueError) as exc:
                    failure = {"case_id": c["id"], "error": str(exc)[:400]}
                    result["failures"].append(failure)
                    h.db.execute("INSERT INTO thread_cleanup VALUES(?,?) ON CONFLICT(case_id) DO UPDATE SET details=excluded.details", (c["id"], dumps(failure)))
            day_ago = stamp((datetime.fromisoformat(at)-timedelta(hours=resolve().get('maintenance.collector_cache_hours'))).isoformat())
            h.db.execute("DELETE FROM mail_snapshots WHERE created_at<=?", (day_ago,))
            h.db.execute("INSERT INTO meta VALUES('thread_cache_maintenance',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (dumps(dict(result, at=at)),))
            h._thread_dirty = False
    finally:
        h._thread_sweeping = False
    return result


def sanitize_backup(db, at=None):
    from .storage import CURRENT_SCHEMA
    if db.execute('PRAGMA user_version').fetchone()[0]!=CURRENT_SCHEMA:
        raise ValueError('Only current-format backups are supported')
    db.row_factory = __import__("sqlite3").Row
    db.execute("PRAGMA foreign_keys=ON")
    from .approvals import lifecycle as approval_lifecycle
    from types import SimpleNamespace
    approval_lifecycle(SimpleNamespace(db=db), at)
    for table in ("thread_messages", "thread_attachments", "mail_snapshots"):
        db.execute(f"DELETE FROM {table}")
    for r in db.execute("SELECT id,details FROM cases WHERE status='closed'").fetchall():
        pending=any(pending_approval(json.loads(a[0])) for a in db.execute('SELECT details FROM approvals WHERE case_id=?',(r['id'],)))
        if not pending and json.loads(r["details"]).get("expires_at", "9999") <= stamp(at or now()):
            expire_case(db, r["id"])
    from .evidence import prune
    from types import SimpleNamespace
    prune(SimpleNamespace(db=db), at)
    db.commit()
    db.execute("VACUUM")
    db.execute('PRAGMA journal_mode=DELETE')


def snapshot(h, operation, payload=None, sid=None):
    if operation == "put":
        with h.write():
            h.db.execute("INSERT INTO mail_snapshots VALUES(?,?,?)", (payload["SnapshotId"], now(), dumps(payload)))
        return {"id": payload["SnapshotId"]}
    row = h.db.execute("SELECT details,created_at FROM mail_snapshots WHERE id=?", (sid,)).fetchone()
    cutoff = stamp((datetime.fromisoformat(now())-timedelta(hours=resolve().get('maintenance.collector_cache_hours'))).isoformat())
    if not row or row["created_at"] <= cutoff:
        raise ValueError("Mail snapshot expired or missing; capture a fresh bounded snapshot")
    return json.loads(row["details"])
