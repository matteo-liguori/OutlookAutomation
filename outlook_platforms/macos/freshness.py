"""Recent, stable native Inbox watermarks using the Windows freshness fields."""
from datetime import datetime,timedelta,timezone
import time
from .native import execute,request_for


def probe(mailbox,inbox_id,*,freshness_minutes=1440,interval=15,max_wait=30,runner=execute,sleeper=time.sleep,clock=lambda:datetime.now(timezone.utc)):
    if not 1<=freshness_minutes<=1440 or not 1<=interval<=300 or not 0<=max_wait<=900:
        raise ValueError('Invalid freshness timing')
    started=clock();marks=[];errors=[];latest=None
    while True:
        at=clock()
        request=request_for('mail',mailbox,(at-timedelta(minutes=freshness_minutes)).isoformat(),(at+timedelta(minutes=5)).isoformat(),1,selected_inbox_id=inbox_id)
        result=runner(request)
        latest=result.get('items',[None])[0] if result.get('items') else None
        if result.get('status')!='ok' or result.get('read_state_preserved') is not True:errors.append(result.get('code','native-read-state-unverified'))
        marks.append((latest['id'],latest['received_at']) if latest else None)
        stable=len(marks)>=2 and marks[-1] is not None and marks[-1]==marks[-2] and not errors
        if stable or (clock()-started).total_seconds()>=max_wait:break
        sleeper(min(interval,60,max_wait-(clock()-started).total_seconds()))
    checked=clock();received=datetime.fromisoformat(latest['received_at']) if latest else None
    current=bool(stable and checked-timedelta(minutes=freshness_minutes)<=received<=checked+timedelta(minutes=5))
    return dict(Store=mailbox,CheckedAt=checked.isoformat(),Folder='Inbox',Status='current' if current else 'unavailable' if errors else 'empty' if not latest else 'not-stable',Current=current,
        Samples=len(marks),WaitedSeconds=round((checked-started).total_seconds(),1),
        LatestReceived=latest['received_at'] if latest else None,LatestSubject=latest['subject'] if latest else None,
        LatestEntryId=latest['id'] if latest else None,LatestStoreId='outlook-macos:'+mailbox.casefold(),
        LatestFolder=mailbox+'/Inbox',InboxLatestReceived=latest['received_at'] if latest else None,
        LatestAgeMinutes=round((checked-received).total_seconds()/60,1) if received else None,
        Diagnosis='Recent stable native Inbox watermark.' if current else 'Recent stable Inbox mail was not established.',
        DiagnosticErrors=errors,Transport=dict(kind='outlook-apple-events',synchronization_completion_verified=False),
        Detail='Two matching recent native Inbox samples; this is separate from Send/Receive completion.',
        ReadStatePreserved=not errors and result.get('read_state_preserved') is True)
