"""Immutable native collector pages using the existing monitor snapshot contract."""
from datetime import datetime, timedelta, timezone
import re
import uuid

from .native import execute, request_for


def collect_page(h, mailbox, since, until, inbox_id, *, page_size=150, max_scan=750,
                 offset=0, snapshot_id='', runner=execute):
    from monitor.threadcache import snapshot
    if not 1 <= page_size <= 500 or not 1 <= max_scan <= 5000 or not 0 <= offset <= 5000:
        raise ValueError('Invalid collector page/scan limit or offset')
    first,last=(datetime.fromisoformat(value) for value in (since,until))
    if first.utcoffset() is None or last.utcoffset() is None or not timedelta(0)<=last-first<=timedelta(days=7):
        raise ValueError('Collector requires an ordered, timezone-aware window of at most seven days')
    first,last=first.astimezone(timezone.utc),last.astimezone(timezone.utc)
    request = request_for('mail', mailbox, first.isoformat(), (last if last>first else last+timedelta(milliseconds=1)).isoformat(), min(max_scan, 100), selected_inbox_id=inbox_id)
    since, until = first.isoformat(),last.isoformat()
    if snapshot_id:
        if not re.fullmatch('[a-f0-9]{32}', snapshot_id):
            raise ValueError('Invalid snapshot ID')
        saved = snapshot(h, 'get', sid=snapshot_id)
        if (saved['Store'], datetime.fromisoformat(saved['Since']), datetime.fromisoformat(saved['Until'])) != (mailbox, datetime.fromisoformat(since), datetime.fromisoformat(until)):
            raise ValueError('Snapshot store/window mismatch')
        if saved.get('InboxId') != str(inbox_id):
            raise ValueError('Snapshot Inbox binding mismatch')
    else:
        if offset:
            raise ValueError('Continuation requires a SnapshotId')
        request.update(capture=True, limit=max_scan,end=(last+timedelta(milliseconds=1)).isoformat())
        started = datetime.now(timezone.utc).isoformat()
        result = runner(request)
        if result.get('status') != 'ok':
            raise ValueError('Native snapshot failed: ' + result.get('code', 'unknown'))
        store_id = 'outlook-macos:' + mailbox.casefold()
        saved = dict(Store=mailbox, StoreId=store_id, Since=since, Until=until,
                     InboxId=str(inbox_id), SnapshotId=uuid.uuid4().hex, StartedAt=started,
                     Scanned=result['matched_count'], Complete=bool(result.get('capture_complete')),
                     Errors=result.get('errors', []), Items=[dict(
                         Store=mailbox, StoreId=store_id, EntryId=item['id'],
                         InternetMessageId=item.get('internet_message_id', ''),
                         ConversationId=item.get('conversation_id', ''),
                         MessageClass=item.get('message_class', 'outlook-macos:message'),
                         Received=item['received_at'], Subject=item['subject'], SenderEmail=item.get('sender_email', '')
                     ) for item in result['items'] if first<=datetime.fromisoformat(item['received_at'])<=last])
        snapshot(h, 'put', saved)
    rows = saved['Items']
    if offset > len(rows):
        raise ValueError('Offset exceeds saved snapshot')
    end = min(offset + page_size, len(rows))
    more = end < len(rows)
    return dict(Store=mailbox, StoreId=saved['StoreId'], Since=saved['Since'], Until=saved['Until'],
                SnapshotId=saved['SnapshotId'], StartedAt=saved['StartedAt'],
                FirstId=rows[0]['EntryId'] if rows else '', Offset=offset,
                NextOffset=end if more else None, Scanned=saved['Scanned'], Count=end-offset,
                Newest=rows[0]['Received'] if rows else None,
                Complete=not more and saved['Complete'], Errors=saved['Errors'], Items=rows[offset:end])


def ingest_snapshot(h, run_id, source, first_page, *, freshness=None, page_size=150):
    from monitor.collect import import_pages
    if first_page['Offset']!=0:raise ValueError('Ingestion must start with page zero')
    saved_mailbox = first_page['Store']
    from monitor.sources import MAIL_STORES
    if MAIL_STORES.get(source) != saved_mailbox:
        raise ValueError('Native collection does not match the configured source')
    from monitor.threadcache import snapshot
    saved = snapshot(h, 'get', sid=first_page['SnapshotId'])
    pages = [first_page]
    while pages[-1]['NextOffset'] is not None:
        pages.append(collect_page(h, saved_mailbox, saved['Since'], saved['Until'], saved['InboxId'],
                                  page_size=page_size, offset=pages[-1]['NextOffset'], snapshot_id=saved['SnapshotId']))
    # Existing freshness policy is retained; no freshness proof means unavailable,
    # even when the bounded snapshot itself is complete.
    return import_pages(h, run_id, source, pages, first_page['StartedAt'], freshness)
