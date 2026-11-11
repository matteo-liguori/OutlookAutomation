"""Native actions governed by exact SQLite approvals and durable attempt journals."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import uuid

from .native import SCRIPT

OPERATIONS=SCRIPT.with_name('operations.js')


def native_operation(request, runner=subprocess.run):
    source=SCRIPT.read_text()+'\n'+OPERATIONS.read_text()
    try:
        p=runner(['/usr/bin/osascript','-l','JavaScript','-e',source,json.dumps(request)],capture_output=True,text=True,timeout=60)
    except subprocess.TimeoutExpired:
        return dict(status='blocked',code='native-operation-timeout',mutation_may_have_occurred=True)
    if p.returncode:return dict(status='blocked',code='native-operation-transport-failed',mutation_may_have_occurred=True)
    try:return json.loads(p.stdout)
    except ValueError:return dict(status='blocked',code='invalid-native-operation-result',mutation_may_have_occurred=True)


def require_approval(h,approval_id):
    from monitor.approvals import find
    from monitor.model import now
    kind,_,grant=find(h,approval_id)
    if kind!='one-off' or grant.get('status')!='approved' or grant.get('suspended') or grant.get('review_requested_at'):
        raise ValueError('An active, exact one-off approval is required')
    if not grant.get('granted_at') or not grant.get('next_review_at') or grant['next_review_at']<=now() or (grant.get('expires_at') and grant['expires_at']<=now()):
        raise ValueError('Approval is expired or needs review')
    return grant


def allowed(grant,action):
    actions=grant.get('scope',{}).get('actions')
    if actions is not None and action not in actions:raise ValueError('Approval does not cover '+action)


def journal(h,key,state=None):
    row=h.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    previous=json.loads(row[0]) if row else None
    if state is not None:
        h.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(state)))
    return previous


def prepared_message(message,attachment_root):
    from monitor.profile import PROFILE
    if message['sender'].casefold() not in {address.casefold() for address in PROFILE['mail'].values()}:
        raise ValueError('Sender is not the configured mailbox')
    prepared={key:message[key] for key in ('sender','subject','body')}
    for field in ('to','cc','bcc'):
        values=message[field]
        if not isinstance(values,list) or any(not isinstance(address,str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',address) for address in values):
            raise ValueError('Use explicit SMTP recipient addresses')
        prepared[field]=values
    if not prepared['to'] or not isinstance(prepared['body'],str) or not prepared['subject'] or any(c in prepared['subject'] for c in '\r\n'):
        raise ValueError('Invalid approved message')
    prepared['attachments']=[];root=Path(attachment_root).resolve()
    for approved in message['attachments']:
        if not isinstance(approved,dict) or not approved.get('sha256'):
            raise ValueError('Approved attachments require path and SHA-256')
        path=Path(approved['path']).absolute()
        if not path.resolve().is_relative_to(root) or any(node.is_symlink() for node in (path,*path.parents) if node!=root):
            raise ValueError('Attachment escaped the configured private data folder')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest!=approved['sha256']:raise ValueError('Approved attachment content changed')
        prepared['attachments'].append(dict(path=str(path),name=path.name,size=path.stat().st_size,sha256=digest))
    return prepared


def verify_attachment_bytes(message,inbox_id,draft_folder_id,draft_id,root,runner):
    """Outlook reports encoded sizes; compare decoded bytes, then discard exports."""
    from .attachments import download_attachment
    for index,attachment in enumerate(message['attachments'],1):
        asset=download_attachment(message['sender'],inbox_id,draft_folder_id,draft_id,
            dict(index=index,name=attachment['name'],size=attachment['size']),
            private_root=root,allow_large=True,runner=runner)
        if asset.get('status')!='ok':raise ValueError('Draft attachment export failed: '+asset.get('code','unknown'))
        path=Path(asset['path'])
        try:
            if asset['bytes']!=attachment['size'] or asset['sha256']!=attachment['sha256']:
                raise ValueError('Approved draft attachment bytes changed')
        finally:
            path.unlink();path.parent.rmdir()


def send_approved(h,approval_id,inbox_id,draft_folder_id,*,save_only=False,display=False,runner=native_operation,attachment_root=None):
    from monitor.paths import data_root
    from monitor.model import digest
    grant=require_approval(h,approval_id)
    allowed(grant,'save-draft' if save_only or display else 'send')
    if not grant.get('message'):raise ValueError('Exact approved message required')
    if grant.get('message_hash')!=digest(grant['message']):raise ValueError('Approval message integrity mismatch')
    message=prepared_message(grant['message'],attachment_root or data_root())
    key='native-send:'+approval_id
    with h.write():
        state=journal(h,key)
        if state and state.get('fingerprint')!=grant['message_hash']:raise ValueError('Approval identity changed')
        if state and state['stage']=='submitted':return dict(state['result'],duplicate_prevented=True)
        if state and state['stage'] not in {'draft-saved'}:raise ValueError('Prior native attempt needs reconciliation; not resubmitting')
        if not state:
            state=dict(stage='creating',fingerprint=grant['message_hash'],operation_id=uuid.uuid4().hex)
            journal(h,key,state)
    base=dict(mailbox=message['sender'],folder_id=str(inbox_id),draft_folder_id=str(draft_folder_id),message=message,authorized=True,allow_duplicate=grant.get('scope',{}).get('allow_duplicate') is True)
    if state['stage']=='creating':
        result=runner(dict(base,action='draft-create',display=display))
        with h.write():
            state.update(stage='draft-saved' if result.get('status')=='ok' else 'uncertain',result=result)
            if result.get('id'):state['draft_id']=result['id']
            journal(h,key,state)
        if result.get('status')!='ok':return result
    if save_only or display:
        verify_attachment_bytes(message,inbox_id,draft_folder_id,state['draft_id'],attachment_root or data_root(),runner)
        if display and not state['result'].get('displayed'):
            shown=runner(dict(base,action='draft-verify',object_id=state['draft_id'],display=True))
            if shown.get('status')!='ok':return shown
            state['result']=dict(state['result'],displayed=shown.get('displayed') is True)
        return dict(state['result'],EntryId=state['draft_id'],Store=message['sender'],attachment_bytes_verified=True)
    require_approval(h,approval_id)
    prepared_message(grant['message'],attachment_root or data_root())
    verified=runner(dict(base,action='draft-verify',object_id=state['draft_id']))
    if verified.get('status')!='ok':return verified
    verify_attachment_bytes(message,inbox_id,draft_folder_id,state['draft_id'],attachment_root or data_root(),runner)
    with h.write():
        current=journal(h,key)
        if current['stage']!='draft-saved':raise ValueError('Concurrent submission prevented')
        state.update(stage='submitting');journal(h,key,state)
    result=runner(dict(base,action='draft-send',object_id=state['draft_id'],expected_modification=verified['modification']))
    result=dict(result,EntryId=state['draft_id'],Store=message['sender'])
    with h.write():
        state.update(stage='submitted' if result.get('status')=='ok' else 'uncertain' if result.get('mutation_may_have_occurred',True) else 'draft-saved',result=result);journal(h,key,state)
    return result


def update_approved(h,approval_id,inbox_id,*,runner=native_operation,attachment_root=None):
    from monitor.paths import data_root
    from monitor.model import digest
    grant=require_approval(h,approval_id);allowed(grant,'update-draft');scope=grant.get('scope',{})
    if not scope.get('draft_id') or not scope.get('draft_folder_id') or not grant.get('message'):
        raise ValueError('Draft update requires exact draft identity and approved wording')
    if grant.get('message_hash')!=digest(grant['message']):raise ValueError('Approval message integrity mismatch')
    message=prepared_message(grant['message'],attachment_root or data_root())
    request=dict(action='draft-update',authorized=True,mailbox=message['sender'],folder_id=str(inbox_id),draft_folder_id=str(scope['draft_folder_id']),object_id=str(scope['draft_id']),message=message)
    key='native-update:'+approval_id
    with h.write():
        prior=journal(h,key)
        if prior:
            if prior['stage']=='complete':return dict(prior['result'],duplicate_prevented=True)
            raise ValueError('Prior update needs reconciliation')
        journal(h,key,dict(stage='attempting'))
    result=runner(request)
    if result.get('status')=='ok':
        verify_attachment_bytes(message,inbox_id,scope['draft_folder_id'],scope['draft_id'],attachment_root or data_root(),runner)
    with h.write():
        journal(h,key,dict(stage='complete' if result.get('status')=='ok' else 'uncertain',result=result))
        if result.get('status')=='ok':journal(h,'native-send:'+approval_id,dict(stage='draft-saved',fingerprint=grant['message_hash'],draft_id=str(scope['draft_id']),result=result))
    return dict(result,EntryId=str(scope['draft_id']),Store=message['sender'])


def move_approved(h,approval_id,inbox_id,*,runner=native_operation):
    from monitor.profile import PROFILE
    grant=require_approval(h,approval_id);scope=grant.get('scope',{})
    needed={'mailbox','source_folder_id','entry_id','destination_folder_id','internet_message_id'}
    if not needed<=scope.keys() or scope.get('operation')!='move-message':raise ValueError('Exact source/destination move approval required')
    if scope['mailbox'] not in PROFILE['mail'].values():raise ValueError('Move mailbox does not match the profile')
    key='native-move:'+approval_id
    with h.write():
        state=journal(h,key)
        if state:
            if state['stage']=='complete':return dict(state['result'],duplicate_prevented=True)
            raise ValueError('Prior move needs reconciliation; not moving again')
        journal(h,key,dict(stage='attempting'))
    request=dict(action='message-move',authorized=True,mailbox=scope['mailbox'],folder_id=str(inbox_id),source_folder_id=str(scope['source_folder_id']),object_id=str(scope['entry_id']),destination_folder_id=str(scope['destination_folder_id']),internet_message_id=scope['internet_message_id'])
    result=runner(request)
    with h.write():journal(h,key,dict(stage='complete' if result.get('status')=='ok' else 'uncertain',result=result))
    return result


def cli(command,arguments):
    import argparse
    from monitor.core import Harness
    from monitor.profile import PROFILE
    parser=argparse.ArgumentParser(prog='outlook.py '+command)
    parser.add_argument('-Store','--store',required=True)
    parser.add_argument('-To','--to',nargs='+')
    parser.add_argument('-Cc','--cc',nargs='*')
    parser.add_argument('-Bcc','--bcc',nargs='*')
    parser.add_argument('-Subject','--subject')
    parser.add_argument('-Body','--body')
    parser.add_argument('-Attachments','--attachments',nargs='*')
    parser.add_argument('-DisplayOnly','--display-only',action='store_true')
    parser.add_argument('-AllowDuplicate','--allow-duplicate',action='store_true')
    parser.add_argument('-EntryId','--entry-id')
    parser.add_argument('--approval-id',required=True)
    parser.add_argument('--selected-inbox-id')
    parser.add_argument('--drafts-folder-id')
    args=parser.parse_args(arguments)
    from monitor.profile import native_bindings
    bindings=native_bindings(PROFILE,args.store)
    inbox=args.selected_inbox_id or bindings.get('inbox_id');drafts=args.drafts_folder_id or bindings.get('draft_folder_id')
    if not inbox:parser.error('Verified Inbox ID required')
    h=Harness()
    try:
        grant=require_approval(h,args.approval_id)
        message=grant.get('message')
        if message:
            if message['sender'].casefold()!=args.store.casefold():raise ValueError('Store differs from the exact approval')
            for field in ('to','cc','bcc','subject','body'):
                value=getattr(args,field)
                if value is not None and value!=message[field]:raise ValueError('Command differs from approved '+field)
            if args.attachments is not None and [str(Path(path).resolve()) for path in args.attachments]!=[str(Path(item['path']).resolve()) for item in message['attachments']]:raise ValueError('Command differs from approved attachments')
        if args.allow_duplicate and grant.get('scope',{}).get('allow_duplicate') is not True:raise ValueError('Duplicate submission needs explicit approval scope')
        if command=='Move-OutlookMessage':
            scope=grant.get('scope',{})
            if scope.get('mailbox')!=args.store or (args.entry_id and args.entry_id!=str(scope.get('entry_id'))):raise ValueError('Move command differs from the exact approval')
            result=move_approved(h,args.approval_id,inbox)
        elif command=='Update-OutlookStoreDraft':
            if args.entry_id and args.entry_id!=str(grant.get('scope',{}).get('draft_id')):raise ValueError('Draft identity differs from the exact approval')
            result=update_approved(h,args.approval_id,inbox)
        else:
            if not drafts:raise ValueError('Verified Gmail Drafts folder ID required')
            result=send_approved(h,args.approval_id,inbox,drafts,save_only=command=='Save-OutlookStoreDraft',display=args.display_only)
    except (ValueError,KeyError) as error:
        parser.error(str(error))
    finally:h.close()
    print(json.dumps(result,indent=2))
    return 0 if result.get('status')=='ok' else 2
