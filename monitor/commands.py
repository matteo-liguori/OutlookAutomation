import json
import sqlite3
import sys
from pathlib import Path
from .model import dumps,now


def read_payload(path):
    return json.loads(sys.stdin.read().lstrip('\ufeff') if path == '-' else Path(path).read_text(encoding='utf-8-sig'))


def handle_init(h, args, payload, kind):
    cmd, op = args.command, getattr(args, "operation", None)
    return {"database": str(h.path), "schema": h.db.execute("PRAGMA user_version").fetchone()[0]}


def handle_guide(h, args, payload, kind):
    from .guidance import guide, help_for
    if args.command == 'help':
        return help_for(args.target)
    return guide(args.topic)


def handle_doctor(h, args, payload, kind):
    cmd, op = args.command, getattr(args, "operation", None)
    return h.page(kind, [h.doctor()])


def handle_config(h,args,payload,kind):
    from .configuration import resolve,review,location,lock,paths,read_yaml,assign,defaults,validate,plain,atomic,dump
    card=h.case(args.case_id) if getattr(args,'case_id',None) else None
    config=resolve(card=card)
    if args.operation=='show':return dict(values=config.values,**({'origins':config.origins} if args.explain else {}))
    if args.operation=='validate':return dict(valid=True)
    if args.operation=='review':return dict(reviews=review(ident=(payload or {}).get('id'),decision=(payload or {}).get('decision')))
    folder=location()
    if folder is None:raise ValueError('No private customisation file selected')
    with lock(folder):
        path,_=paths(folder);user=read_yaml(path)
        from .configuration import leaves
        values=dict(leaves(defaults()))
        if args.key not in values:raise ValueError('Unknown setting: '+args.key)
        assign(user,args.key,values[args.key]);validate(plain(user));atomic(path,dump(user))
    return dict(reset=args.key)


def handle_outlook(h,args,payload,kind):
    if args.operation=='status' and getattr(args,'details',False):
        from outlook_platforms.api import status
        return status(details=True)
    from outlook_platforms.api import invoke
    result = invoke(args.operation,payload,h)
    if args.operation == 'read' and payload and payload.get('case_id') and isinstance(result, dict):
        from .approvals import sending_notice
        result = dict(result, **sending_notice(h, payload['case_id']))
    return result


def handle_backup(h, args, payload, kind):
    cmd, op = args.command, getattr(args, "operation", None)
    return {"backups": h.backup()}


def handle_schedule(h, args, payload, kind):
    cmd, op = args.command, getattr(args, "operation", None)
    from .timing import plan, verify

    return (
        verify(payload)
        if op == "verify"
        else plan(args.date, args.times, args.purpose, args.at, args.fold)
    )


def handle_calendar(h, args, payload, kind):
    cmd, op = args.command, getattr(args, "operation", None)
    from .google_calendar import prepare, normalize, ingest

    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    if op == "google-prepare":
        return prepare(config, args.date, h.run(args.run_id)["started_at"] if args.run_id else None)
    if op == "google-validate":
        return normalize(payload, config)
    return ingest(h, args.run_id, payload, config)


def handle_filing(h, args, payload, kind):
    cmd, op = args.command, getattr(args, "operation", None)
    from .filing import record, status

    if op == "record":
        return record(h, payload)
    return h.page(kind, status(h,getattr(h,'_query_at',None)), limit=args.limit)


def handle_briefing(h, args, payload, kind):
    cmd, op = args.command, getattr(args, "operation", None)
    session = None
    if cmd == 'briefing' and not (args.task or args.case_ids or args.message_input or getattr(args,'cursor',None)):
        from .activation import startup
        session = startup(h, getattr(h,'_query_at',None) or now())
    if getattr(args, 'user_active', False) or getattr(args, 'setup_chat', False):
        from .activation import arm
        arm(h, user_active=getattr(args, 'user_active', False))
    if cmd == 'briefing' and not getattr(args,'cursor',None):
        from .configuration import sync
        from .profile import PROFILE
        sync(profile=PROFILE)
    if cmd == 'briefing':
        from .activation import reconcile
        reconcile(h)
    if cmd == "briefing" and (args.task or args.case_ids or args.message_input):
        from .briefing import relevant

        message = (
            read_payload(args.message_input)
            if args.message_input
            else None
        )
        items=relevant(h,args.task,args.case_ids,message)
        from .cards import upkeep_prompt
        selected={i['case'] for i in items if i.get('reason')=='task-relevant'}
        from .card_input import pending
        items=pending(h,selected)+items
        from .guidance import context_prompts
        items=context_prompts(items)+upkeep_prompt(items,getattr(h,'_query_at',None) or now(),selected,'related-evidence' if message else 'explicit-task')+items
        return h.page(kind,items,limit=args.limit)
    at=getattr(h,'_query_at',None) or now()
    if cmd=='briefing':
        from .briefing import compact
        from .configuration import notices
        items=notices()+compact(h,at)
    else:
        items=h.current_items(briefing=False,at=at)
    result = h.page(kind,items,limit=args.limit)
    if session:
        result['session_start'] = session
    return result


def handle_action(h, args, payload, kind):
    cmd, op = args.command, getattr(args, "operation", None)
    from .scheduling import review

    return review(h, payload)


def handle_review(h, args, payload, kind):
    from .reviews import next_card, complete
    at = getattr(h, '_query_at', None) or now()
    if args.operation == 'next':
        return next_card(h, at, args.cursor, args.limit)
    return complete(h, payload, at)


def handle_activation(h, args, payload, kind):
    from .activation import complete, defer, detach
    return {'defer':defer, 'detach':detach, 'complete':complete}[args.operation](h, payload)


def handle_case(h, args, payload, kind):
    cmd, op = args.command, getattr(args, "operation", None)
    if op in {'edit', 'note-ack'}:
        from .card_input import apply
        return apply(h, payload, acknowledge=op == 'note-ack')
    if op=='render':
        from .cards import render
        return render(h,args.id)
    if op in {"merge", "split"}:
        from .organize import organize

        return organize(h, op, payload)
    if op == "update":
        return h.update(payload)
    if op == "show":
        from .cards import page as card_page
        return card_page(h,args.id,args.cursor)
    if op == "search":
        rows = [h.case(r[0]) for r in h.db.execute(
            "SELECT id FROM cases WHERE instr(lower(id||title||position||details),lower(?))>0 OR id IN (SELECT case_id FROM actions WHERE instr(lower(details),lower(?))>0) ORDER BY id",
            (args.query,args.query)).fetchall()]
        return h.page(kind,rows,limit=args.limit)


def handle_evidence(h, args, payload, kind):
    cmd, op = args.command, getattr(args, "operation", None)
    if op == "put":
        return h.put_evidence(payload)
    row = h.db.execute("SELECT * FROM evidence WHERE id=?", (args.id,)).fetchone()
    if not row:
        raise ValueError("Unknown evidence")
    with h.write():
        h.touch("evidence", args.id)
    result = dict(row)
    if json.loads(row["metadata"]).get("kind") == "collector-index":
        result["resolved_messages"] = [
            h.decode_message(item) for item in json.loads(row["body"])["Items"]
        ]
    return h.page(kind, [result])


def handle_approval(h, args, payload, kind):
    cmd, op = args.command, getattr(args, "operation", None)
    if cmd == 'approval':
        from . import approvals
        if op == 'put': return approvals.put(h, payload)
        if op == 'review': return approvals.review(h, payload)
        if op == 'flag': return approvals.flag(h, payload)
        if op == 'show': return h.page(kind, [approvals.find(h, args.id)[2]])
        return h.page(kind, approvals.items(h, getattr(args, 'kind', None), getattr(h, '_query_at', None)), limit=args.limit)
    tables = {
        "context": "context",
        "issue": "issues",
        "roundup": "roundup",
    }
    table = tables[cmd]
    if op == "put":
        return h.put_record(cmd, payload)
    if cmd == "context":
        rows = [
            dict(r)
            for r in h.db.execute(
                "SELECT * FROM context WHERE instr(lower(entity||details),lower(?))>0 ORDER BY id",
                (args.id,),
            )
        ]
    else:
        rows = [dict(r) for r in h.db.execute(f"SELECT * FROM {table} WHERE id=?", (args.id,))]
    return h.page(kind, rows, limit=args.limit)


def handle_improvement(h, args, payload, kind):
    cmd, op = args.command, getattr(args, "operation", None)
    from .improvements import add, checkpoint, items, review

    if op == "checkpoint":
        return checkpoint(h, payload)
    if op == "add":
        return add(h, payload)
    if op == "review":
        return review(h, payload)
    return h.page(
        kind, items(h, getattr(args, "status", None), getattr(args, "id", None)), limit=args.limit
    )


def handle_run(h, args, payload, kind):
    cmd, op = args.command, getattr(args, "operation", None)
    if op == "reflect":
        from .improvements import reflect

        return reflect(h, args.id, payload)
    if op == "reflection":
        rows = h.db.execute("SELECT details FROM reflections WHERE run_id=?", (args.id,)).fetchall()
        return h.page(kind, [json.loads(r[0]) for r in rows], limit=args.limit)
    if op == "start":
        from .guidance import guide
        if args.activated:
            from .activation import arm
            arm(h)
        result = h.start_run(args.activated)
        result['guidance'] = [guide('pass-receipts')]
        if args.activated:
            result['guidance'].append(guide('activation'))
        return result
    if op == "record":
        return (
            h.record_runs(args.id, payload)
            if isinstance(payload, list)
            else h.record_run(args.id, payload)
        )
    if op == "ingest":
        from .collect import import_pages

        result = import_pages(
            h,
            args.id,
            payload["source"],
            payload["pages"],
            payload["started_at"],
            payload.get("freshness"),
        )
        if result.get('status') != 'current':
            from .guidance import guide
            result = dict(result, guidance=guide('mail-access'))
        return result
    if op == "candidates":
        h.run(args.id)
        with h.write():
            h.touch("run", args.id)
        if h.run(args.id)['details'].get('working_expired'):
            return h.page(kind,[{'technical_detail_expired':True,'instruction':'Start a fresh pass; unresolved candidates remain in pending_work.'}],limit=args.limit)
        return h.page(
            kind,
            [
                h.decode_message(r[0])
                for r in h.db.execute(
                    "SELECT details FROM candidates WHERE run_id=? ORDER BY id", (args.id,)
                )
            ],
            limit=args.limit,
        )
    if op == "status":
        return h.run_status(args.id)
    if op == "finalize":
        return h.finalize(args.id)


def handle_report(h, args, payload, kind):
    cmd, op = args.command, getattr(args, "operation", None)
    return h.page(kind, h.report_items(args.id), limit=args.limit)


def handle_maintenance(h, args, payload, kind):
    from .maintenance import clean, status, register_file, record_warning

    if args.operation == "threads":
        from .threadcache import sweep
        return sweep(h)

    if args.operation == "register":
        register_file(h, **payload)
        return {"registered": True}
    try:
        return status(h) if args.operation == "status" else clean(h, dry_run=args.dry_run)
    except (OSError, ValueError, sqlite3.Error) as exc:
        if not getattr(args, "dry_run", True):
            record_warning(h, exc)
        raise


def handle_thread(h, args, payload, kind):
    from .threadcache import put_message, find_message, folder, require_open, register_attachment, snapshot, attachment_path

    op = args.operation
    if op == "put":
        return put_message(h, payload)
    if op == "find":
        return find_message(h, payload)
    if op == "attachment":
        return register_attachment(h, payload)
    if op == "attachment-path":
        return {"path": str(attachment_path(h, payload["case_id"], payload["name"], payload["attachment_id"]))}
    if op == "path":
        require_open(h, args.id)
        return {"path": str(folder(h, args.id))}
    if op == "messages":
        require_open(h, args.id)
        return h.page(kind, [{"message_key": r[0], **json.loads(r[1])} for r in h.db.execute("SELECT message_key,metadata FROM thread_messages WHERE case_id=? ORDER BY cached_at,message_key", (args.id,))], limit=args.limit)
    if op == "snapshot-put":
        return snapshot(h, "put", payload)
    if op == "snapshot-get":
        return snapshot(h, "get", sid=args.id)
    raise ValueError("Unknown thread operation")


def handle_refresh(h, args, payload, kind):
    if args.operation == "put":
        with h.write():
            h.db.execute("INSERT INTO meta VALUES('outlook:last-refresh',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (dumps(payload),))
            from .live import record
            record(h,'outlook:refresh',dict(payload,status='current' if payload.get('success') else 'unavailable',complete=payload.get('success') is True),payload.get('completed_at') or payload.get('started_at'))
        return {"recorded": True}
    row = h.db.execute("SELECT value FROM meta WHERE key='outlook:last-refresh'").fetchone()
    return json.loads(row[0]) if row else None


def handle_health(h,args,payload,kind):
    from .live import items
    return h.page(kind,items(h,getattr(h,'_query_at',None)),limit=args.limit)


HANDLERS = {
    "config": handle_config,
    "outlook": handle_outlook,
    "review": handle_review,
    "activation": handle_activation,
    "help": handle_guide,
    "guide": handle_guide,
    "health": handle_health,
    "thread": handle_thread,
    "refresh": handle_refresh,
    "init": handle_init,
    "doctor": handle_doctor,
    "backup": handle_backup,
    "maintenance": handle_maintenance,
    "schedule": handle_schedule,
    "calendar": handle_calendar,
    "filing": handle_filing,
    "briefing": handle_briefing,
    "cases": handle_briefing,
    "action": handle_action,
    "case": handle_case,
    "evidence": handle_evidence,
    "context": handle_approval,
    "approval": handle_approval,
    "issue": handle_approval,
    "roundup": handle_approval,
    "improvement": handle_improvement,
    "run": handle_run,
    "report": handle_report,
}


def execute(h,args):
    from .configuration import snapshot, resolve
    case_id=getattr(args,"case_id",None)
    with snapshot(resolve(card=h.case(case_id) if case_id and h else None)):
        return _scoped_execute(h,args)


def _scoped_execute(h,args):
    prior={name:getattr(h,name) for name in ('_page_cursor','_query_at') if h is not None and hasattr(h,name)}
    try:
        result = _execute(h,args)
        if h is not None and isinstance(result, dict):
            from .activation import reminder
            note = reminder(h)
            if note:
                result = dict(result, reminder=note)
        return result
    finally:
        if h is not None:
            for name in ('_page_cursor','_query_at'):
                if name in prior:setattr(h,name,prior[name])
                elif hasattr(h,name):delattr(h,name)


def _execute(h, args):
    cmd, op = args.command, getattr(args, "operation", None)
    payload = (
        read_payload(args.input)
        if getattr(args, "input", None)
        else None
    )
    kind = ":".join(
        [
            cmd,
            op or "",
            getattr(args, "id", ""),
            getattr(args, "query", ""),
            getattr(args, "scope", ""),
        ]
    )
    if cmd == "improvement":
        kind += ":" + str(getattr(args, "status", None))
    if cmd == "briefing":
        kind += ":" + dumps([args.task, args.case_ids, args.message_input])
    if h is not None:
        h._page_cursor=args.cursor
        from .views import cursor_time
        h._query_at=cursor_time(args.cursor) if args.cursor and args.cursor.startswith('live:') else now()
    if args.cursor:
        if args.cursor.startswith('card:'):
            if cmd!='case' or op!='show':raise ValueError('Card cursors apply only to case show')
            return handle_case(h,args,payload,kind)
        readable = {
            "review": {"next"},
            "briefing": {None},
            "cases": {"list"},
            "doctor": {None},
            "case": {"show", "search"},
            "context": {"get"},
            "approval": {"show", "list"},
            "issue": {"show"},
            "roundup": {"show"},
            "evidence": {"show"},
            "filing": {"status"},
            "improvement": {"list", "show"},
            "run": {"candidates", "reflection"},
            "report": {"data"},
            "thread": {"messages"},
            "health": {None},
        }
        if op not in readable.get(cmd, set()):
            raise ValueError("Paging cursors apply only to paginated read commands")
        if args.cursor.startswith('live:'):
            return HANDLERS[cmd](h,args,payload,kind)
        raise ValueError('Saved-page cursors are retired; restart the current query')
    result = HANDLERS[cmd](h, args, payload, kind)
    if h is not None and getattr(args, "input", None) not in (None, "-") and cmd != "maintenance":
        path = Path(args.input).resolve()
        if (
            path.is_relative_to((h.path.parent / "scratch").resolve())
            and path.suffix.lower() == ".json"
        ):
            from .maintenance import register_file

            try:
                register_file(
                    h,
                    path,
                    run_id=(
                        getattr(args, "id", None) if cmd == "run" else getattr(args, "run_id", None)
                    ),
                )
            except (OSError, ValueError, sqlite3.Error) as exc:
                from .maintenance import record_warning

                record_warning(h, exc)
    return result
