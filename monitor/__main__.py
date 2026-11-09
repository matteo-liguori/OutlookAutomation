import argparse
import json
import sys
import sqlite3
from automation_paths import data_root

from .core import DEFAULT_DB, Harness


def parser():
    p = argparse.ArgumentParser(description="Current thread cards, scheduled actions and bounded monitoring.")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--format", choices=["json", "markdown"], default="markdown")
    p.add_argument("--cursor")
    p.add_argument("--limit", type=int, default=50)
    sub = p.add_subparsers(dest="command", required=True)
    ui = sub.add_parser('ui', help='Open the local read-only card grid and action timeline')
    ui.add_argument('--port', type=int, default=0, help='Local port; default selects a free port')
    ui.add_argument('--no-browser', action='store_true')
    sub.add_parser("init")
    from .guidance import GUIDES
    sub.add_parser("guide").add_argument("topic", choices=sorted(GUIDES))
    sub.add_parser("help", help="Retrieve instructions for a topic or command").add_argument("target", nargs="*")
    briefing = sub.add_parser("briefing")
    briefing.add_argument("--task", default="")
    briefing.add_argument("--case", action="append", default=[], dest="case_ids")
    briefing.add_argument("--message-input")
    briefing.add_argument("--user-active", action="store_true", help="Start the pending title/follow-up checklist after a user reply")
    briefing.add_argument("--setup-chat", action="store_true", help="Check title/follow-ups at scheduled startup without inferring user presence")
    activation = sub.add_parser('activation').add_subparsers(dest='operation', required=True)
    activation.add_parser('complete').add_argument('--input', required=True)
    activation.add_parser('defer').add_argument('--input', required=True)
    activation.add_parser('detach').add_argument('--input', required=True)
    sub.add_parser("doctor")
    sub.add_parser('health')
    sub.add_parser("backup")
    from outlook_platforms.api import available_operations
    outlook = sub.add_parser('outlook', help='Profile-selected Outlook access').add_subparsers(dest='operation', required=True)
    for operation in available_operations():
        command=outlook.add_parser(operation)
        command.add_argument('--input', help='JSON payload, or - for stdin')
        if operation=='status':command.add_argument('--details',action='store_true',help='Installation/backend diagnostics')
    maintenance = sub.add_parser("maintenance").add_subparsers(dest="operation", required=True)
    maintenance.add_parser("status")
    maintenance.add_parser("clean").add_argument("--dry-run", action="store_true")
    maintenance.add_parser("register").add_argument("--input", required=True)
    maintenance.add_parser("threads")
    thread = sub.add_parser("thread").add_subparsers(dest="operation", required=True)
    for operation in ("put", "find", "attachment", "attachment-path", "snapshot-put"):
        thread.add_parser(operation).add_argument("--input", required=True)
    for operation in ("messages", "path", "snapshot-get"):
        thread.add_parser(operation).add_argument("id")
    refresh = sub.add_parser("refresh").add_subparsers(dest="operation", required=True)
    refresh.add_parser("put").add_argument("--input", required=True)
    refresh.add_parser("get")
    schedule = sub.add_parser("schedule").add_subparsers(dest="operation", required=True)
    sp = schedule.add_parser("plan")
    sp.add_argument("--date")
    sp.add_argument("--time", action="append", dest="times")
    sp.add_argument("--purpose", choices=["continuation", "reminder"], default="continuation")
    sp.add_argument("--at")
    sp.add_argument("--fold", type=int, choices=[0, 1])
    schedule.add_parser("verify").add_argument("--input", required=True)
    google = sub.add_parser("calendar").add_subparsers(dest="operation", required=True)
    gp = google.add_parser("google-prepare")
    gp.add_argument("--date")
    gp.add_argument("--run-id")
    gp.add_argument("--config", default=str(data_root() / 'monitor/google-calendar-config.json'))
    for name in ["google-validate", "google-ingest"]:
        gp = google.add_parser(name)
        gp.add_argument("--input", required=True)
        gp.add_argument("--config", default=str(data_root() / 'monitor/google-calendar-config.json'))
        if name == "google-ingest":
            gp.add_argument("--run-id", required=True)
    filing = sub.add_parser("filing").add_subparsers(dest="operation", required=True)
    filing.add_parser("status")
    filing.add_parser("record").add_argument("--input", required=True)
    cases = sub.add_parser("cases").add_subparsers(dest="operation", required=True)
    cases.add_parser("list")
    case = sub.add_parser("case").add_subparsers(dest="operation", required=True)
    case.add_parser("show").add_argument("id")
    case.add_parser('render').add_argument('id')
    search = case.add_parser("search")
    search.add_argument("query")
    case.add_parser("update").add_argument("--input", required=True)
    for name in ["merge", "split"]:
        case.add_parser(name).add_argument("--input", required=True)
    action = sub.add_parser("action").add_subparsers(dest="operation", required=True)
    action.add_parser("review").add_argument("--input", required=True)
    reviews = sub.add_parser('review').add_subparsers(dest='operation', required=True)
    reviews.add_parser('next')
    reviews.add_parser('complete').add_argument('--input', required=True)
    approvals = sub.add_parser('approval').add_subparsers(dest='operation', required=True)
    al = approvals.add_parser('list')
    al.add_argument('--kind', choices=['standing', 'one-off'])
    approvals.add_parser('show').add_argument('id')
    for name in ('put', 'review', 'flag'):
        approvals.add_parser(name).add_argument('--input', required=True)
    for name in ["evidence", "context", "issue", "roundup"]:
        ops = sub.add_parser(name).add_subparsers(dest="operation", required=True)
        ops.add_parser("put").add_argument("--input", required=True)
        ops.add_parser("get" if name == "context" else "show").add_argument("id")
    run = sub.add_parser("run").add_subparsers(dest="operation", required=True)
    start = run.add_parser("start")
    start.add_argument("--activated", action="store_true")
    for name in ["status", "finalize"]:
        run.add_parser(name).add_argument("id")
    record = run.add_parser("record")
    record.add_argument("id")
    record.add_argument("--input", required=True)
    ingest = run.add_parser("ingest")
    ingest.add_argument("id")
    ingest.add_argument("--input", required=True)
    pending = run.add_parser("candidates")
    pending.add_argument("id")
    reflect = run.add_parser("reflect")
    reflect.add_argument("id")
    reflect.add_argument("--input", required=True)
    run.add_parser("reflection").add_argument("id")
    improvements = sub.add_parser("improvement").add_subparsers(dest="operation", required=True)
    improvements.add_parser("list").add_argument(
        "--status", choices=["proposed", "accepted", "deferred", "implemented", "rejected"]
    )
    improvements.add_parser("show").add_argument("id")
    improvements.add_parser("add").add_argument("--input", required=True)
    improvements.add_parser("checkpoint").add_argument("--input", required=True)
    improvements.add_parser("review").add_argument("--input", required=True)
    report = sub.add_parser("report").add_subparsers(dest="operation", required=True)
    report.add_parser("data").add_argument("id")
    return p


from .commands import execute


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    if not 1 <= args.limit <= 100:
        raise SystemExit("--limit must be 1..100")
    h = None
    try:
        if args.command == 'ui':
            from .ui import serve
            if not 0 <= args.port <= 65535:
                raise ValueError('Port must be between 0 and 65535')
            serve(args.db, args.port, not args.no_browser)
            return
        pure = args.command in {"schedule", "guide", "help"} or (
            args.command == "calendar"
            and args.operation != "google-ingest"
            and not getattr(args, "run_id", None)
        )
        h = None if pure else Harness(args.db, create=args.command == "init")
        result = execute(h, args)
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if args.format == "markdown":
            rendered = markdown(result)
        print(rendered)
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
    finally:
        if h:
            h.close()


def markdown(result):
    if result.get('complete') is True and result.get('remaining') == 0:
        return 'No periodic reviews due.'
    if "items" not in result:
        return "```json\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n```"
    lines = [
        f"Items {result['offset'] + 1 if result['items'] else 0}–{result['offset'] + len(result['items'])} of {result['total']}.",
        "",
    ]
    brief = any(i.get('type') == 'command_hint' for i in result['items'])
    group = None
    for item in result["items"]:
        if item.get('type') == 'briefing_section':
            lines.extend(['', '**'+item['title']+'**', ''])
            continue
        if brief and not any(i.get('type') == 'briefing_section' for i in result['items']):
            label = ('Open threads' if item.get('type') == 'thread_watch' else
                     'Commands' if item.get('type') == 'command_hint' else 'Due work and alerts')
            if label != group:
                lines.extend(['', '**'+label+'**', ''])
                group = label
        if item.get('type') == 'thread_watch':
            lines.append(f"- **{item['case']} — {item['title']}**: {item['summary']}")
        elif item.get('type') == 'review_due':
            lines.append(f"- **{item['case']} — {item['title']}**")
        elif item.get('type') in {'review_hint', 'review_instructions'}:
            lines.append(item['instruction'])
        elif item.get('type') == 'due_action':
            line=f"- **{item['action']}** [{item['status']}]: {item['next_step']} (due {item.get('due_at') or 'now'})"
            if item.get('deadline'): line += ' Deadline: '+item['deadline']
            lines.append(line)
        elif item.get('instruction') and (item.get('type','').endswith('_prompt') or item.get('type') == 'command_hint'):
            lines.append(item['instruction'])
        elif item.get("type") == "filing_checkpoint":
            from datetime import datetime
            from .core import LONDON

            def local(value):
                return (
                    datetime.fromisoformat(value).astimezone(LONDON).strftime("%d/%m/%Y %H:%M:%S")
                    if value
                    else "never verified"
                )

            lines.append(
                f"- **Filing — {item['source']}**: last successful {local(item['last_successful_at'])}; filed through {local(item['filed_through'])}. Review Inbox {local(item['since'])} to {local(item['until'])} Europe/London (maximum seven days). Latest attempt: {item['last_attempt_status'] or 'none'}."
                + (
                    " Older gap is outside the seven-day scope."
                    if item["older_gap_outside_scope"]
                    else ""
                )
            )
        elif "next_step" in item:
            lines.append(
                f"- **{item.get('action',item.get('id'))}** [{item['status']}] {item['next_step']} Owner: {item['owner']}. Review: {item.get('next_review') or 'closed'}."
                + (f" Urgency: {item['urgency']}." if item.get("urgency") else "")
                + (f" Deadline: {item['deadline']}." if item.get("deadline") else "")
                + (f" **Follow-up due: {item['follow_up_due_at']}** ({item['follow_up']['reason']})." if item.get("follow_up_due") else "")
            )
        elif "position" in item:
            lines.append(
                f"- **{item.get('id',item.get('case'))} — {item.get('title','')}**: {item['position']}"
            )
        elif item.get("type") == "decision":
            lines.append(f"- Decision {item['id']}: {item['text']}")
        else:
            lines.append("```json\n" + json.dumps(item, ensure_ascii=False, indent=2) + "\n```")
    if result["next_cursor"]:
        lines.extend(
            [
                "",
                "More results: pass `--cursor "
                + result["next_cursor"]
                + "` before the same command.",
            ]
        )
    if result.get('reminder'):
        lines.insert(0, result['reminder'])
    if result.get('session_start'):
        lines.insert(0, json.dumps(result['session_start'],ensure_ascii=False))
    output = "\n".join(lines)
    if len(output) > 12000:
        return "```json\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n```"
    return output


if __name__ == "__main__":
    main()
