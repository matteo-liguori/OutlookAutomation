/* Command bridge. Credentials stay in Thunderbird; writes are journalled. */
const HOST = 'outlookautomation_thunderbird_pilot';
const MAX_SCAN = 5000;
let port;
let writeBusy = false;
const READS = new Set(['status','accounts','folders','query','read','calendar-list','calendar-query','calendar-read','reconcile-move','reconcile-trash']);

function check(condition, code) {
  if (!condition) throw new Error(code);
}
function fingerprint(value) {
  if(Array.isArray(value))return '['+value.map(fingerprint).join(',')+']';
  if(value!==null && typeof value==='object')return '{'+Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+fingerprint(value[key])).join(',')+'}';
  return JSON.stringify(value);
}
function sameFingerprint(stored, request) {
  try{return fingerprint(JSON.parse(stored))===fingerprint(request);}catch(e){return false;}
}
async function account(r) {
  check(typeof r.mailbox === 'string' && typeof r.account_id === 'string', 'exact-account-required');
  const a = await messenger.accounts.get(r.account_id, true);
  check(a && a.identities.some(i => i.email.toLowerCase() === r.mailbox.toLowerCase()), 'account-identity-mismatch');
  return a;
}
async function folder(r, id) {
  const f = await messenger.folders.get(id);
  check(f.accountId === r.account_id, 'folder-account-mismatch');
  return f;
}
async function message(r) {
  const h = await messenger.messages.get(r.message_id);
  check(h.folder && h.folder.accountId === r.account_id, 'message-account-mismatch');
  if (r.source_folder_id) check(h.folder.id === r.source_folder_id, 'message-parent-changed');
  if (r.internet_message_id) check(h.headerMessageId === r.internet_message_id, 'internet-message-id-changed');
  return h;
}
function header(h) {
  return {id:h.id, internet_message_id:h.headerMessageId, subject:h.subject, date:h.date,
    author:h.author, recipients:h.recipients, cc:h.ccList || [], read:h.read, folder:h.folder};
}
async function query(r, folderId, internetId) {
  const q = {accountId:r.account_id, messagesPerPage:50};
  if (folderId) q.folderId = folderId;
  if (internetId) q.headerMessageId = internetId;
  if (r.subject) q.subject = r.subject;
  if (r.query) q.fullText = r.query;
  if (r.unread_only) q.unread = true;
  if (r.start) q.fromDate = new Date(r.start);
  if (r.end) q.toDate = new Date(r.end);
  const out = []; let scanned = 0; let page = await messenger.messages.query(q);
  while (true) {
    scanned += page.messages.length;
    for (const h of page.messages) {
      if ((!r.subject || r.subject_contains || h.subject === r.subject) && (!internetId || h.headerMessageId === internetId)) out.push(h);
    }
    if (!page.id) return {messages:out, complete:true, scanned};
    if (scanned >= MAX_SCAN) {
      await messenger.messages.abortList(page.id);
      return {messages:out, complete:false, scanned};
    }
    page = await messenger.messages.continueList(page.id);
  }
}
async function previous(r) {
  check(typeof r.approval_id === 'string' && r.approval_id.length > 0, 'approval-id-required');
  const key = 'pilot:' + r.action + ':' + r.approval_id;
  const identity = fingerprint(r);
  const old = (await messenger.storage.local.get(key))[key];
  if (old) {
    check(sameFingerprint(old.fingerprint,r), 'approval-request-changed');
    check(old.stage === 'complete', 'prior-write-needs-reconciliation');
    return {...old.result, duplicate_prevented:true};
  }
  return null;
}
async function once(r, action, metadata={}) {
  const old=await previous(r); if(old)return old;
  const key = 'pilot:' + r.action + ':' + r.approval_id;
  const identity = fingerprint(r);
  await messenger.storage.local.set({[key]:{stage:'attempting', fingerprint:identity,...metadata}});
  const result = await action();
  check(!result.status || result.status==='ok',result.code || 'write-not-verified');
  await messenger.storage.local.set({[key]:{stage:'complete', fingerprint:identity, result,...metadata}});
  return result;
}
async function verifyMove(r, subject, sourceId, beforeRead, restoreRead, destinationBefore=[], sourceBeforeCount=1) {
  for(let attempt=0;attempt<20;attempt++) {
    const moved=await query({...r,subject},r.destination_folder_id,r.internet_message_id);
    const source=await query({...r,subject},sourceId,r.internet_message_id);
    const candidates=moved.messages.filter(m=>!destinationBefore.includes(m.id));
    const added=moved.messages.length===destinationBefore.length+1 && candidates.length===1;
    // Gmail can expose one logical message in both Inbox and Sent, then merge
    // those labels into the existing Trash copy. No extra physical copy is
    // required for recoverable deletion. Never change an ambiguous old copy's
    // read flag; report any differing flags explicitly in the receipt.
    const merged=r.action==='trash-message' && moved.messages.length>0;
    if(moved.complete && (added || merged) && source.complete && source.messages.length===sourceBeforeCount-1) {
      let after=added ? candidates[0] : moved.messages[0];
      if(restoreRead && added && after.read!==beforeRead){await messenger.messages.update(after.id,{read:beforeRead});after=await messenger.messages.get(after.id);}
      const preserved=added ? after.read===beforeRead : moved.messages.every(m=>m.read===beforeRead);
      check(preserved || merged,'move-read-state-not-preserved');
      return {status:'ok',message:header(after),read_state_preserved:preserved,readback_attempts:attempt+1,
        warnings:preserved ? [] : ['Provider merged recovery copies with differing read flags; existing Trash flags were left unchanged.'],
        ...(r.action==='trash-message' ? {recoverable:true,destination_merged:!added} : {})};
    }
    await new Promise(resolve=>setTimeout(resolve,250));
  }
  throw new Error('move-not-verified');
}
async function dispatch(r) {
  if (r.action === 'status') return {status:'ok', client:await messenger.runtime.getBrowserInfo(),
    transport:'thunderbird-native-messaging', api:{read:!!messenger.messages.getFull,
      move:!!messenger.messages.move, send:!!(messenger.compose && messenger.compose.sendMessage)},
    write_scope:'exact-approval-broker', calendar:!!messenger.harnessCalendar,write_busy:writeBusy};
  if (r.action === 'accounts') return {status:'ok', accounts:(await messenger.accounts.list(false)).map(a =>
    ({id:a.id,type:a.type,name:a.name,identities:a.identities.map(i=>({id:i.id,email:i.email,name:i.name}))}))};
  if(r.action.startsWith('calendar-')) {
    check(!!messenger.harnessCalendar,'calendar-extension-not-installed');
    if(['calendar-create','calendar-update','calendar-delete'].includes(r.action))return once(r,()=>messenger.harnessCalendar.execute(r));
    return messenger.harnessCalendar.execute(r);
  }
  const a = await account(r);
  if (r.action === 'folders') {
    const out=[]; const visit=f=>{out.push({id:f.id,name:f.name,path:f.path,accountId:f.accountId,specialUse:f.specialUse}); for(const child of f.subFolders||[])visit(child);};
    visit(a.rootFolder); return {status:'ok', folders:out};
  }
  if (r.action === 'query') {
    check(r.subject || r.internet_message_id || (r.start && r.end), 'bounded-query-required');
    if (r.folder_id) await folder(r,r.folder_id);
    const result=await query(r,r.folder_id,r.internet_message_id);
    return {...result,messages:result.messages.map(header),status:'ok'};
  }
  if (r.action === 'read') {
    const before=await message(r); const full=await messenger.messages.getFull(before.id);
    const after=await message(r);check(before.read===after.read,'read-state-changed');
    return {status:'ok',message:header(after),full,read_state_preserved:true};
  }
  if(r.action==='send-message') {
    const cached=await previous(r);if(cached)return cached;
    const ids=a.identities.filter(i=>i.email.toLowerCase()===r.mailbox.toLowerCase());
    check(ids.length===1,'sender-identity-ambiguous');
    check(Array.isArray(r.to) && r.to.length>0 && typeof r.subject==='string' && typeof r.body==='string','invalid-envelope');
    return once(r,async()=>{
      const tab=await messenger.compose.beginNew({identityId:ids[0].id,to:r.to,cc:r.cc,bcc:r.bcc,
        subject:r.subject,plainTextBody:r.body,isPlainText:true,deliveryFormat:'plaintext'});
      const d=await messenger.compose.getComposeDetails(tab.id);
      check(d.identityId===ids[0].id && d.subject===r.subject && d.plainTextBody.trimEnd()===r.body.trimEnd() &&
        fingerprint(d.to)===fingerprint(r.to) && fingerprint(d.cc)===fingerprint(r.cc) && fingerprint(d.bcc)===fingerprint(r.bcc),
        'approved-composer-changed');
      return {status:'ok',submission:await messenger.compose.sendMessage(tab.id,{mode:'sendNow'}),delivery_verified:false};
    });
  }
  if(r.action==='move-message' || r.action==='trash-message') {
    const cached=await previous(r);if(cached)return cached;
    const h=await message(r);
    check(r.source_folder_id && r.internet_message_id,'exact-move-identity-required');
    await folder(r,r.destination_folder_id);check(h.folder.id!==r.destination_folder_id,'same-source-destination');
    const existing=await query(r,r.destination_folder_id,r.internet_message_id);
    check(existing.complete && (r.action==='trash-message' || existing.messages.length===0),'destination-already-contains-message');
    const sourceBefore=await query(r,h.folder.id,r.internet_message_id);
    check(sourceBefore.complete && sourceBefore.messages.length>0 && (r.action==='trash-message' || sourceBefore.messages.length===1),'source-message-identity-ambiguous');
    const destinationBefore=existing.messages.map(m=>m.id);
    return once(r,async()=>{
      await messenger.messages.move([h.id],r.destination_folder_id);
      return {...await verifyMove(r,null,h.folder.id,h.read,true,destinationBefore,sourceBefore.messages.length),
        recoverable:r.action==='trash-message'};
    },{before_read:h.read,destination_before_ids:destinationBefore,source_before_count:sourceBefore.messages.length});
  }
  if(r.action==='reconcile-move' || r.action==='reconcile-trash') {
    const key='pilot:'+(r.action==='reconcile-trash' ? 'trash-message' : 'move-message')+':'+r.approval_id;
    const old=(await messenger.storage.local.get(key))[key];
    check(!!old,'prior-move-journal-missing');
    const original=JSON.parse(old.fingerprint);
    for(const key of ['account_id','mailbox','source_folder_id','destination_folder_id','internet_message_id']) {
      check(original[key]===r[key],'prior-move-request-changed');
    }
    check(typeof old.before_read==='boolean','prior-read-state-not-recorded');
    const result=await verifyMove(original,null,original.source_folder_id,old.before_read,false,old.destination_before_ids || [],old.source_before_count || 1);
    // This only reads message state and reconciles the journal; it never moves
    // again or changes a message's read flag.
    await messenger.storage.local.set({[key]:{...old,stage:'complete',result}});
    return {...result,reconciled:true,move_command_reissued:false};
  }
  throw new Error('unsupported-bridge-action');
}
function connect() {
  port=messenger.runtime.connectNative(HOST);
  port.onMessage.addListener(envelope=>{
    const channel=port, isWrite=!READS.has(envelope.request.action);
    if(isWrite && writeBusy) {
      channel.postMessage({id:envelope.id,result:{status:'blocked',code:'client-write-in-progress',mutation_may_have_occurred:false}});
      return;
    }
    if(isWrite)writeBusy=true;
    // A timed-out operation may still complete in the client. Keep writes busy
    // until it settles, but let exact readbacks and status run independently.
    const task=dispatch(envelope.request).finally(()=>{if(isWrite)writeBusy=false;});
    let deadline;
    const timeout=new Promise((_,reject)=>{deadline=setTimeout(()=>reject(new Error('client-operation-timeout')),40000);});
    Promise.race([task,timeout]).then(result=>channel.postMessage({id:envelope.id,result}),
      e=>channel.postMessage({id:envelope.id,result:{status:'blocked',code:e.message,mutation_may_have_occurred:isWrite}}))
      .catch(()=>{}).finally(()=>clearTimeout(deadline));
  });
  port.onDisconnect.addListener(()=>{setTimeout(connect,1500);});
}
connect();
