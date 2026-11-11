"""Byte-verified attachment transfer, transport inspection and freshness."""
import base64
from datetime import datetime,timedelta,timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import time
import uuid

from monitor.paths import data_root

CHUNK=192*1024
MAX_FILE=100*1024*1024


def uploaded_attachments(message,base,call,runner):
    files=[];root=data_root().resolve()
    for approved in message.get('attachments',[]):
        path=Path(approved['path']).absolute();resolved=path.resolve()
        if not resolved.is_relative_to(root) or any(p.is_symlink() for p in (path,*path.parents) if p!=root):raise ValueError('Attachment must be a regular file inside the private data directory')
        size=path.stat().st_size
        if size>MAX_FILE:raise ValueError('Attachment exceeds the 100 MiB transfer limit')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest!=approved['sha256']:raise ValueError('Approved attachment changed')
        token=uuid.uuid4().hex;offset=0
        with path.open('rb') as stream:
            while True:
                chunk=stream.read(CHUNK)
                r=call(dict(base,action='attachment-upload',upload_id=token,offset=offset,total_size=size,data=base64.b64encode(chunk).decode()),runner)
                if r.get('status')!='ok' or r.get('received')!=offset+len(chunk):raise ValueError('Attachment upload did not verify')
                offset+=len(chunk)
                if offset==size:break
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('Attachment changed during upload')
        files.append(dict(name=path.name,size=size,sha256=digest,content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream',upload_id=token))
    return files


def attachment_specs(message):
    result=[];root=data_root().resolve()
    for item in message.get('attachments',[]):
        path=Path(item['path']).absolute()
        if not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in (path,*path.parents) if p!=root):raise ValueError('Attachment escaped the private data directory')
        if path.stat().st_size>MAX_FILE or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('Approved attachment size/content changed')
        result.append(dict(name=path.name,size=path.stat().st_size,sha256=item['sha256'],content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream'))
    return result


def download_attachment(base,locator,part,call,runner,*,allow_large=False,target=None):
    r=call(dict(base,**locator,action='attachment-open',part_name=part['part_name'],allow_large=allow_large),runner)
    if r.get('status')!='ok':return r
    name=part['name'];safe=re.sub(r'[^\w. -]','_',name.replace('\\','/').rsplit('/',1)[-1]).strip(' .')[:160] or 'attachment.bin'
    folder=data_root()/'thunderbird/downloads'/uuid.uuid4().hex
    if target is None:
        folder.mkdir(parents=True,mode=0o700);target=folder/safe
    else:
        target=Path(target);target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not target.resolve().is_relative_to(data_root().resolve()) or any(p.is_symlink() for p in (target,*target.parents) if p!=data_root().resolve()):raise ValueError('Attachment target escaped private storage')
    temporary=target.with_name(target.name+'.'+uuid.uuid4().hex+'.partial')
    digest=hashlib.sha256();offset=0
    try:
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:
            while True:
                chunk=call(dict(base,**locator,action='attachment-chunk',download_id=r['download_id'],offset=offset),runner)
                if chunk.get('status')!='ok' or chunk.get('offset')!=offset:raise ValueError('Attachment chunk failed verification')
                content=base64.b64decode(chunk['data'],validate=True)
                if len(content)>CHUNK or offset+len(content)>r['size']:raise ValueError('Attachment transfer size mismatch')
                stream.write(content);digest.update(content);offset+=len(content)
                if chunk.get('eof'):break
                if not content:raise ValueError('Empty nonfinal attachment chunk')
        if offset!=r['size'] or digest.hexdigest()!=r['sha256']:raise ValueError('Downloaded attachment hash/size mismatch')
        temporary.replace(target)
    finally:
        if temporary.exists():temporary.unlink()
    return dict(status='ok',path=str(target.resolve()),name=name,size=offset,bytes=offset,sha256=digest.hexdigest(),part_name=part['part_name'],downloaded=True)


def transport_request(base,binding,**values):
    ids=[binding[k] for k in ('sent_folder_id','draft_folder_id') if binding.get(k)]
    return dict(base,transport_folder_ids=ids,sent_folder_id=binding.get('sent_folder_id'),inbox_folder_id=binding.get('selected_inbox_id'),**values)


def duplicate_scan(base,binding,payload,call,runner):
    days=payload.get('lookback_days',30)
    if type(days) is not int or not 1<=days<=3650:raise ValueError('Invalid duplicate lookback_days')
    if not payload.get('subject'):raise ValueError('Duplicate check requires an exact subject')
    end=datetime.now(timezone.utc);start=end-timedelta(days=days)
    r=call(transport_request(base,binding,action='duplicates',start=start.isoformat(),end=end.isoformat(),
        subject=payload['subject'],body=payload.get('body',''),recipients=payload.get('recipients',[]),
        max_scan=payload.get('max_scan',500),limit=payload.get('limit',50)),runner)
    from .commands import row
    rows=[]
    for item in r.get('rows',[]):
        h=item['message'];rows.append(dict(Store=base['mailbox'],Folder=h['folder']['path'],EntryId=row(h,base['mailbox'])['EntryId'],TransientId=h['id'],
            InternetMessageId=h['internet_message_id'],Subject=h['subject'],Received=h['date'],
            BodyMatch=item['body_match'],RecipientOverlap=item['recipient_overlap']))
    return dict(Complete=r.get('complete',False),Rows=rows,Errors=r.get('errors',[]),Scanned=r.get('scanned',0),**({'code':r['code']} if 'code' in r else {}))


def refresh(base,binding,payload,call,runner,inbox_only=False):
    ids=[binding['selected_inbox_id']]
    if not inbox_only:ids+=list(dict.fromkeys(binding[k] for k in ('sent_folder_id','draft_folder_id','trash_folder_id') if binding.get(k)))
    return call(dict(base,action='client-refresh',folder_ids=ids,timeout_ms=min(15000,max(1000,int(payload.get('wait_seconds',8))*1000))),runner)


def freshness(base,binding,payload,call,runner):
    refreshed=refresh(base,binding,{},call,runner,inbox_only=True)
    checked=datetime.now(timezone.utc);minutes=payload.get('freshness_minutes',1440)
    if type(minutes) is not int or not 1<=minutes<=525600:raise ValueError('Invalid freshness_minutes')
    end=checked+timedelta(seconds=5);start=checked-timedelta(minutes=minutes)
    observations=[];errors=[]
    if refreshed.get('success'):
        interval=payload.get('interval_seconds',1)
        if not isinstance(interval,(int,float)) or not 0<=interval<=30:raise ValueError('Invalid freshness interval')
        for attempt in range(2):
            if attempt and interval:time.sleep(interval)
            result=call(dict(base,action='query',folder_id=binding['selected_inbox_id'],start=start.isoformat(),end=end.isoformat()),runner)
            if result.get('status')!='ok' or not result.get('complete'):errors.append(result.get('code','Inbox sample incomplete'));break
            newest=max(result['messages'],key=lambda m:(m['date'],m['id']),default=None)
            observations.append((newest['internet_message_id'],newest['date']) if newest else None)
    stable=len(observations)==2 and observations[0]==observations[1] and observations[0] is not None
    current=bool(refreshed.get('provider_callback_verified') and stable)
    return dict(Store=base['mailbox'],Current=current,Status='current' if current else 'unknown',CheckedAt=datetime.now(timezone.utc).isoformat(),
        LatestStoreId='thunderbird:'+base['mailbox'].casefold(),LatestReceived=observations[-1][1] if observations and observations[-1] else None,
        LatestInternetMessageId=observations[-1][0] if observations and observations[-1] else None,
        ProviderCallbackVerified=bool(refreshed.get('provider_callback_verified')),StableSamples=len(observations),
        Reason='' if current else 'No verified recent stable Inbox after provider callback',Errors=errors,refresh=refreshed)
