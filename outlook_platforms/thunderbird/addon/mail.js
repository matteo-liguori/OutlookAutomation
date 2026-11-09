/* Mail capabilities implemented through Thunderbird APIs; no UI automation. */
const MAIL_MAX_FILE=100*1024*1024, MAIL_CHUNK=192*1024;
const mailUploads=new Map(), mailDownloads=new Map();
function mailSubject(value) {return String(value || '').replace(/^(?:(?:re|fw|fwd)\s*:\s*)+/gi,'').replace(/\s+/g,' ').trim().toLowerCase();}
function mailBody(value) {return String(value || '').replace(/\r\n?/g,'\n').trim();}
function mailWriteIdentity(r) {
  const clean=JSON.parse(JSON.stringify(r));
  for(const item of clean.attachments || [])delete item.upload_id;
  return fingerprint(clean);
}
async function mailHash(bytes) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(v=>v.toString(16).padStart(2,'0')).join('');
}
function mailEncode(bytes) {
  let text='';for(let i=0;i<bytes.length;i+=8192)text+=String.fromCharCode(...bytes.subarray(i,i+8192));return btoa(text);
}
function mailDecode(text) {const raw=atob(text);return Uint8Array.from(raw,c=>c.charCodeAt(0));}
function mailExpire(map) {for(const [key,value] of map)if(Date.now()-value.at>10*60*1000)map.delete(key);}
async function mailAddresses(values) {
  const out=[];
  for(const text of values || [])for(const address of await messenger.messengerUtilities.parseMailboxString(text)) {
    if(address.email)out.push(address.email.toLowerCase());
  }
  return out.sort();
}
async function mailOwn(h,r) {
  return fingerprint(await mailAddresses([h.author]))===fingerprint([r.mailbox.toLowerCase()]);
}
async function mailText(full) {
  const text=[],html=[];
  function visit(p) {if(p.name)return;if(p.contentType==='text/plain' && p.body)text.push(p.body);if(p.contentType==='text/html' && p.body)html.push(p.body);for(const child of p.parts || [])visit(child);}
  visit(full);
  // Exchange may store a plain-text draft as HTML-only MIME. Use Thunderbird's
  // own parser, never regex tag stripping, for its readable body and readback.
  return mailBody(text.length ? text.join('\n') : (await Promise.all(html.map(body=>messenger.messengerUtilities.convertToPlainText(body,{flowed:false})))).join('\n'));
}
async function mailRawHash(id) {
  const file=await messenger.messages.getRaw(id,{data_format:'File'});
  check(file.size<=MAIL_MAX_FILE,'message-too-large-for-byte-verification');
  return mailHash(await file.arrayBuffer());
}
async function mailAttachmentInfo(id,hashes=false) {
  const list=await messenger.messages.listAttachments(id);const result=[];
  for(const attachment of list) {
    const item={name:attachment.name,part_name:attachment.partName,size:attachment.size,content_type:attachment.contentType};
    if(hashes) {
      const file=await messenger.messages.getAttachmentFile(id,attachment.partName);
      check(file.size<=MAIL_MAX_FILE,'attachment-too-large');item.size=file.size;item.sha256=await mailHash(await file.arrayBuffer());
    }
    result.push(item);
  }
  return result;
}
async function mailContent(r,h,hashes=false) {
  // Save/copy callbacks can return a stale read flag. Establish a fresh baseline
  // immediately before the read, then require it to remain unchanged.
  h=await message({...r,message_id:h.id,source_folder_id:h.folder.id,internet_message_id:h.headerMessageId});
  const full=await messenger.messages.getFull(h.id);
  const after=await message({...r,message_id:h.id,source_folder_id:h.folder.id,internet_message_id:h.headerMessageId});
  check(after.read===h.read,'read-state-changed');
  const headers=full.headers || {};
  const result={header:header(after),body:await mailText(full),to:await mailAddresses(h.recipients),
    cc:await mailAddresses(h.ccList),bcc:await mailAddresses(h.bccList?.length ? h.bccList : headers.bcc || []),
    attachments:await mailAttachmentInfo(h.id,hashes)};
  result.subject=(await messenger.messengerUtilities.decodeMimeHeader('subject',headers.subject || [h.subject]))[0] || h.subject;
  result.header.subject=result.subject;
  result.in_reply_to=(headers['in-reply-to'] || []).join(' ').trim();
  return result;
}
async function mailMatches(r,h,attachments=false) {
  if(mailSubject(h.subject)!==mailSubject(r.subject) || !await mailOwn(h,r))return null;
  const data=await mailContent(r,h,attachments);
  if(!r.related_subjects && data.subject!==r.subject)return null;
  const all=[...data.to,...data.cc,...data.bcc];
  data.reply_match=!r.reply_internet_message_id || data.in_reply_to.includes('<'+r.reply_internet_message_id+'>') || data.in_reply_to===r.reply_internet_message_id;
  data.priority_match=['high','highest'].includes(h.priority)===!!r.high_importance;
  data.body_match=r.body!==undefined && mailBody(data.body)===mailBody(r.body);
  data.recipient_overlap=(r.recipients || r.to || []).some(v=>all.includes(v.toLowerCase()));
  data.envelope_match=fingerprint(data.to)===fingerprint((r.to || []).map(v=>v.toLowerCase()).sort()) &&
    fingerprint(data.cc)===fingerprint((r.cc || []).map(v=>v.toLowerCase()).sort()) &&
    fingerprint(data.bcc)===fingerprint((r.bcc || []).map(v=>v.toLowerCase()).sort());
  data.attachments_match=attachments && fingerprint(data.attachments.map(v=>({name:v.name,size:v.size,sha256:v.sha256})))===
    fingerprint((r.attachments || []).map(v=>({name:v.name,size:v.size,sha256:v.sha256})));
  return data;
}
async function mailTransportFolders(r,a) {
  const ids=new Set(r.transport_folder_ids || []);
  const visit=f=>{if((f.specialUse || []).some(k=>['sent','drafts','outbox'].includes(k)))ids.add(f.id);for(const child of f.subFolders || [])visit(child);};
  visit(a.rootFolder);
  // Thunderbird's queue is shared across accounts. Only headers belonging to
  // the configured sender may be read from this explicitly identified Outbox.
  for(const local of await messenger.accounts.list(false))if(local.type==='none') {
    const root=await messenger.accounts.get(local.id,true);
    const outbox=f=>{if((f.specialUse || []).includes('outbox'))ids.add(f.id);for(const child of f.subFolders || [])outbox(child);};outbox(root.rootFolder);
  }
  return [...ids];
}
async function mailScan(r,a,hashes=false) {
  const rows=[];const errors=[];let scanned=0;
  const now=new Date();const related=['duplicates','send-message'].includes(r.action);r={...r,related_subjects:related};
  const bounded={...r,subject:r.subject,subject_contains:true,start:r.start || new Date(now.getTime()-(r.lookback_days || 30)*86400000).toISOString(),end:r.end || now.toISOString()};
  for(const id of await mailTransportFolders(r,a)) {
    let f;
    try {
      f=await messenger.folders.get(id);
      const local=f.accountId!==r.account_id;
      check(!local || (f.specialUse || []).includes('outbox'),'foreign-transport-folder');
      const result=await query({...bounded,account_id:f.accountId,query:undefined},id);
      scanned+=result.scanned;
      if(!result.complete)errors.push('Transport scan limit: '+id);
      for(const h of result.messages) {
        if(!await mailOwn(h,r))continue;
        const exact={...r,allow_local_outbox:local};
        const match=await mailMatches(exact,h,hashes);
        if(match)rows.push(match);
      }
    } catch(error) {errors.push(error.message);}
  }
  return {rows,complete:errors.length===0,errors,scanned};
}
async function mailUploadedFiles(r) {
  const files=[];
  for(const spec of r.attachments || []) {
    const upload=mailUploads.get(spec.upload_id);
    check(upload && upload.account_id===r.account_id && upload.mailbox===r.mailbox && upload.offset===spec.size,'attachment-upload-incomplete');
    const file=new File(upload.parts,spec.name,{type:spec.content_type || 'application/octet-stream'});
    check(file.size===spec.size && await mailHash(await file.arrayBuffer())===spec.sha256,'attachment-bytes-changed');
    files.push(file);
  }
  return files;
}
async function mailVerifyComposer(tab,r,identityId) {
  const details=await messenger.compose.getComposeDetails(tab.id);
  check(details.identityId===identityId,'approved-composer-sender-changed');
  check(details.subject===r.subject,'approved-composer-subject-changed');
  check(mailBody(details.plainTextBody)===mailBody(r.body),'approved-composer-body-changed');
  for(const key of ['to','cc','bcc'])check(fingerprint(await mailAddresses(details[key]))===fingerprint((r[key] || []).map(v=>v.toLowerCase()).sort()),'approved-recipient-changed');
  check(details.overrideDefaultFcc && details.overrideDefaultFccFolder?.id===r.sent_folder_id && !details.additionalFccFolder,'approved-sent-copy-folder-changed');
  check(details.priority===(r.high_importance ? 'high' : 'normal'),'approved-priority-changed');
  const actual=await messenger.compose.listAttachments(tab.id);
  check(actual.length===(r.attachments || []).length,'composer-attachment-count-changed');
  for(let i=0;i<actual.length;i++) {
    const file=await messenger.compose.getAttachmentFile(actual[i].id),wanted=r.attachments[i];
    check(actual[i].name===wanted.name && file.size===wanted.size && await mailHash(await file.arrayBuffer())===wanted.sha256,'composer-attachment-bytes-changed');
  }
}
async function mailDraftState(r) {
  for(const action of ['draft-update','draft-message']) {
    const key='pilot:'+action+':'+r.approval_id;const saved=(await messenger.storage.local.get(key))[key];
    if(!saved)continue;
    check(saved.stage==='complete' && saved.result.draft,'prior-draft-needs-reconciliation');
    const original=JSON.parse(saved.fingerprint);
    for(const key of ['mailbox','account_id','to','cc','bcc','subject','body','high_importance','attachments']) {
      const one=key==='attachments' ? (original[key] || []).map(v=>({name:v.name,size:v.size,sha256:v.sha256})) : original[key];
      const two=key==='attachments' ? (r[key] || []).map(v=>({name:v.name,size:v.size,sha256:v.sha256})) : r[key];
      check(fingerprint(one)===fingerprint(two),'approved-draft-content-changed');
    }
    return saved.result.draft;
  }
  return null;
}
async function mailLocateDraft(r,draft) {
  const matches=await query({...r,subject:undefined},draft.folder_id,draft.internet_message_id);
  check(matches.complete && matches.messages.length===1,'saved-draft-missing-or-ambiguous');
  const h=matches.messages[0];
  check(await mailRawHash(h.id)===draft.raw_sha256,'draft-changed-since-approval');return h;
}
async function mailCheckpoint(r,values) {
  const key='pilot:'+r.action+':'+r.approval_id,old=(await messenger.storage.local.get(key))[key];
  check(old && sameFingerprint(old.fingerprint,r),'write-checkpoint-identity-changed');
  await messenger.storage.local.set({[key]:{...old,...values}});
}
async function mailInspect(old) {
  const details=await messenger.compose.getComposeDetails(old.tab_id),attachments=[];
  for(const item of await messenger.compose.listAttachments(old.tab_id)) {const file=await messenger.compose.getAttachmentFile(item.id);attachments.push({name:item.name,size:file.size,sha256:await mailHash(await file.arrayBuffer())});}
  const value={tab_id:old.tab_id,step:old.step,identity_id:details.identityId,subject:details.subject,body:details.plainTextBody,
    is_plain_text:details.isPlainText,to:await mailAddresses(details.to),cc:await mailAddresses(details.cc),bcc:await mailAddresses(details.bcc),priority:details.priority,attachments};
  return {status:'ok',composer:value,compose_hash:await mailHash(new TextEncoder().encode(fingerprint(value)))};
}
async function mailDetails(r,identityId) {
  // setComposeDetails cannot switch an existing HTML draft to plain text.
  // Supply both representations; Thunderbird selects the editor's actual format.
  const escaped=r.body.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
  check(r.sent_folder_id,'verified-sent-folder-required');
  const sentFolder=await folder(r,r.sent_folder_id);
  return {overrideDefaultFcc:true,overrideDefaultFccFolder:sentFolder,additionalFccFolder:'',identityId,to:r.to,cc:r.cc,bcc:r.bcc,subject:r.subject,plainTextBody:r.body,
    body:'<pre>'+escaped+'</pre>',isPlainText:true,deliveryFormat:'plaintext',priority:r.high_importance ? 'high' : 'normal'};
}
async function mailSaveDraft(r,tab,identityId) {
  await mailVerifyComposer(tab,r,identityId);
  await mailCheckpoint(r,{step:'saving'});
  const saved=await messenger.compose.saveMessage(tab.id,{mode:'draft'});
  check(saved.messages?.length===1,'draft-save-not-verified');
  const returned=saved.messages[0];
  check(returned.folder.accountId===r.account_id && returned.folder.id===r.draft_folder_id,'draft-saved-in-wrong-folder');
  await mailCheckpoint(r,{step:'saved',draft_header:header(returned)});
  let h,after;
  for(let attempt=0;attempt<24;attempt++) {
    try {
      h=await message({...r,message_id:returned.id,source_folder_id:r.draft_folder_id,internet_message_id:undefined});
      after=await mailMatches(r,h,true);
      if(after && ['body_match','envelope_match','attachments_match','reply_match','priority_match'].every(k=>after[k]))break;
    } catch(error) {
      if(!/Message not found|internet-message-id-changed|read-state-changed/.test(error.message))throw error;
    }
    if(attempt<23)await new Promise(resolve=>setTimeout(resolve,250));
  }
  check(after,'saved-draft-subject-or-sender-mismatch');
  for(const key of ['body_match','envelope_match','attachments_match','reply_match','priority_match'])check(after[key],'saved-draft-'+key.replaceAll('_','-')+'-failed');
  const raw=await mailRawHash(h.id);
  if(!r.display_only)await messenger.tabs.remove(tab.id);
  return {status:'ok',draft:{message:after.header,folder_id:h.folder.id,internet_message_id:h.headerMessageId,raw_sha256:raw},attachment_bytes_verified:true,...(r.display_only ? {tab_id:tab.id,displayed:true} : {})};
}
async function mailOwnedComposer(old,r) {
  check(old && old.stage==='attempting','owned-composer-checkpoint-missing');
  if(old.tab_id)return old;
  const original=JSON.parse(old.fingerprint),found=await query({...original,subject:undefined},original.source_folder_id,original.internet_message_id);
  check(found.complete && found.messages.length===1,'original-draft-missing-or-ambiguous');
  const located=await messenger.harnessMail.execute({...r,action:'draft-tab',message_id:found.messages[0].id,internet_message_id:original.internet_message_id});
  check(located.status==='ok' && located.tabs.length===1,'owned-draft-editor-missing-or-ambiguous');
  // A timed-out native opener may have returned no tab checkpoint. Closing
  // this exact saved draft is safe only while the editor is still unmodified.
  const details=await messenger.compose.getComposeDetails(located.tabs[0].id);
  check(details.isModified===false,'uncheckpointed-composer-was-edited');
  return {...old,tab_id:located.tabs[0].id,step:'draft-opened-not-edited'};
}
async function mailCompose(r,a) {
  const cached=await previous(r);if(cached)return cached;
  const identities=a.identities.filter(v=>v.email.toLowerCase()===r.mailbox.toLowerCase());check(identities.length===1,'sender-identity-ambiguous');
  const identityId=identities[0].id;
  let existing=null;
  if(r.action==='draft-update') {
    existing=await message(r);check(existing.folder.id===r.draft_folder_id,'draft-folder-mismatch');
    check(r.expected_raw_sha256 && await mailRawHash(existing.id)===r.expected_raw_sha256,'draft-changed-since-approval');
  } else if(r.action==='send-message') {
    const saved=await mailDraftState(r);if(saved)existing=await mailLocateDraft(r,saved);
  }
  if(r.action==='send-message' && !r.allow_duplicate) {
    const scan=await mailScan(r,a,false);check(scan.complete,'duplicate-scan-incomplete');
    check(!scan.rows.some(v=>v.header.id!==existing?.id && (v.body_match || (!r.body && v.recipient_overlap))),'related-transport-message-exists');
  }
  // Validate upload bytes before creating or changing any draft.
  const files=await mailUploadedFiles(r);
  return once(r,async()=>{
    const details=await mailDetails(r,identityId);
    let tab;
    if(existing) {
      check(!!messenger.harnessMail,'draft-edit-helper-not-installed');
      const openingHash=await mailRawHash(existing.id);
      const opened=await messenger.harnessMail.execute({...r,action:'open-draft',message_id:existing.id,expected_raw_sha256:openingHash});
      check(opened.status==='ok',opened.code || 'draft-open-failed');tab=opened.tab;
      check(await mailRawHash(existing.id)===openingHash,'draft-changed-during-open');
      if((await messenger.messages.get(existing.id)).read!==existing.read)await messenger.messages.update(existing.id,{read:existing.read});
      await messenger.compose.setComposeDetails(tab.id,details);
      for(const attachment of await messenger.compose.listAttachments(tab.id))await messenger.compose.removeAttachment(tab.id,attachment.id);
    } else if(r.reply_message_id) {
      const original=await message({...r,message_id:r.reply_message_id,source_folder_id:r.reply_folder_id,internet_message_id:r.reply_internet_message_id});
      tab=await messenger.compose.beginReply(r.reply_message_id,'replyToSender',details);
      if((await messenger.messages.get(original.id)).read!==original.read)await messenger.messages.update(original.id,{read:original.read});
      for(const attachment of await messenger.compose.listAttachments(tab.id))await messenger.compose.removeAttachment(tab.id,attachment.id);
    } else tab=await messenger.compose.beginNew(details);
    await mailCheckpoint(r,{tab_id:tab.id,step:'composer-open'});
    check(!!messenger.harnessMail,'managed-composer-helper-required');
    const held=await messenger.harnessMail.execute({...r,action:'hold-composer',tab_id:tab.id});check(held.status==='ok',held.code || 'composer-hold-failed');
    for(const file of files)await messenger.compose.addAttachment(tab.id,{file});
    await mailVerifyComposer(tab,r,identityId);
    if(r.action==='draft-message' || r.action==='draft-update') {
      return mailSaveDraft(r,tab,identityId);
    }
    await mailCheckpoint(r,{step:'sending'});
    const submission=await messenger.compose.sendMessage(tab.id,{mode:'sendNow'});
    return {status:'ok',submission,delivery_verified:false,attachment_bytes_verified:true};
  });
}
async function dispatchMail(r,a) {
  if(r.action==='attachment-upload') {
    mailExpire(mailUploads);check(typeof r.upload_id==='string' && /^[a-f0-9]{32}$/.test(r.upload_id),'invalid-upload-id');
    const bytes=mailDecode(r.data);check(bytes.length<=MAIL_CHUNK && r.total_size<=MAIL_MAX_FILE,'attachment-chunk-too-large');
    let upload=mailUploads.get(r.upload_id);
    if(!upload) {check(r.offset===0 && mailUploads.size<12,'upload-capacity-or-offset');upload={account_id:r.account_id,mailbox:r.mailbox,offset:0,parts:[],at:Date.now(),total:r.total_size};mailUploads.set(r.upload_id,upload);}
    check(upload.account_id===r.account_id && upload.mailbox===r.mailbox && upload.offset===r.offset && upload.total===r.total_size && r.offset+bytes.length<=upload.total,'upload-offset-or-account-mismatch');
    check([...mailUploads.values()].reduce((n,v)=>n+v.offset,0)+bytes.length<=200*1024*1024,'upload-memory-capacity');
    upload.parts.push(bytes);upload.offset+=bytes.length;upload.at=Date.now();return {status:'ok',received:upload.offset};
  }
  if(r.action==='attachment-open') {
    mailExpire(mailDownloads);const h=await message(r);const list=await mailAttachmentInfo(h.id);
    check(list.some(v=>v.part_name===r.part_name),'attachment-part-not-found');
    const file=await messenger.messages.getAttachmentFile(h.id,r.part_name);
    check(file.size<=MAIL_MAX_FILE && (file.size<=10*1024*1024 || r.allow_large===true),'large-attachment-opt-in-required');
    const hash=await mailHash(await file.arrayBuffer());const after=await message(r);check(after.read===h.read,'read-state-changed');
    check(mailDownloads.size<12,'download-capacity');const id=crypto.randomUUID().replaceAll('-','');
    mailDownloads.set(id,{file,hash,account_id:r.account_id,mailbox:r.mailbox,read:h.read,at:Date.now(),message_id:h.id});
    return {status:'ok',download_id:id,size:file.size,sha256:hash,name:file.name,read_state_preserved:true};
  }
  if(r.action==='attachment-chunk') {
    mailExpire(mailDownloads);const item=mailDownloads.get(r.download_id);
    check(item && item.account_id===r.account_id && item.mailbox===r.mailbox,'download-account-mismatch');
    check(Number.isInteger(r.offset) && r.offset>=0 && r.offset<=item.file.size,'invalid-download-offset');
    const bytes=new Uint8Array(await item.file.slice(r.offset,r.offset+MAIL_CHUNK).arrayBuffer());item.at=Date.now();
    const h=await message({...r,message_id:item.message_id});check(h.read===item.read,'read-state-changed');
    if(r.offset+bytes.length===item.file.size)mailDownloads.delete(r.download_id);
    return {status:'ok',offset:r.offset,data:mailEncode(bytes),eof:r.offset+bytes.length===item.file.size};
  }
  if(r.action==='duplicates') {
    const scan=await mailScan(r,a,false);return {status:'ok',...scan,rows:scan.rows.slice(0,r.limit || 50).map(v=>({message:v.header,body_match:v.body_match,recipient_overlap:v.recipient_overlap})),complete:scan.complete && scan.rows.length<=(r.limit || 50)};
  }
  if(r.action==='draft-discard') {
    const key='pilot:'+r.original_action+':'+r.approval_id,stored=(await messenger.storage.local.get(key))[key],old=await mailOwnedComposer(stored,r);
    check(['composer-open','draft-opened-not-edited','saving','saved'].includes(old.step),'owned-draft-composer-required');
    const original=JSON.parse(old.fingerprint);check(original.account_id===r.account_id && original.mailbox===r.mailbox,'composer-account-mismatch');
    check((await mailInspect(old)).compose_hash===r.compose_hash,'composer-changed-since-inspection');
    const result=await messenger.harnessMail.execute({...r,action:'discard-composer',tab_id:old.tab_id});check(result.status==='ok',result.code || 'discard-failed');
    await messenger.storage.local.set({[key]:{...old,stage:'abandoned',result}});return result;
  }
  if(r.action==='draft-inspect') {
    const key='pilot:'+r.original_action+':'+r.approval_id,stored=(await messenger.storage.local.get(key))[key],old=await mailOwnedComposer(stored,r);
    const original=JSON.parse(old.fingerprint);check(original.mailbox===r.mailbox && original.account_id===r.account_id,'composer-account-mismatch');
    const inspected=await mailInspect(old);
    inspected.client=await messenger.harnessMail.execute({...r,action:'inspect-composer',tab_id:old.tab_id});return inspected;
  }
  if(r.action==='draft-reconcile') {
    check(!writeBusy,'client-write-in-progress');
    check(['draft-message','draft-update'].includes(r.original_action),'original-draft-action-required');
    const key='pilot:'+r.original_action+':'+r.approval_id,old=(await messenger.storage.local.get(key))[key];
    check(old,'prior-draft-journal-missing');const original=JSON.parse(old.fingerprint);
    if(original.reply_internet_message_id)r={...r,reply_internet_message_id:original.reply_internet_message_id};
    for(const k of ['account_id','mailbox','to','cc','bcc','subject','body','high_importance'])check(fingerprint(original[k])===fingerprint(r[k]),'prior-draft-request-changed');
    check(fingerprint((original.attachments || []).map(v=>({name:v.name,size:v.size,sha256:v.sha256})))===fingerprint((r.attachments || []).map(v=>({name:v.name,size:v.size,sha256:v.sha256}))),'prior-draft-attachment-changed');
    const scan=await mailScan(r,a,true);check(scan.complete,'draft-reconciliation-incomplete');
    const exact=scan.rows.filter(v=>v.body_match && v.envelope_match && v.attachments_match && v.reply_match && v.priority_match);
    check(!exact.some(v=>(v.header.folder.specialUse || []).includes('sent')),'draft-already-sent');
    const drafts=exact.filter(v=>(v.header.folder.specialUse || []).includes('drafts') || v.header.folder.id===r.draft_folder_id);
    if(drafts.length!==1)return {status:'blocked',code:'saved-draft-missing-or-ambiguous',scanned:scan.scanned,candidates:scan.rows.map(v=>({message_id:v.header.id,subject:v.subject,folder_id:v.header.folder.id,body_match:v.body_match,envelope_match:v.envelope_match,attachments_match:v.attachments_match,reply_match:v.reply_match,priority_match:v.priority_match}))};
    const h=drafts[0].header;
    const raw=await mailRawHash(h.id);
    if(messenger.harnessMail) {
      const found=await messenger.harnessMail.execute({...r,action:'draft-tab',message_id:h.id,internet_message_id:h.internet_message_id});
      check(found.status==='ok',found.code || 'draft-editor-inspection-failed');
      for(const tab of found.tabs) {
        const identities=a.identities.filter(v=>v.email.toLowerCase()===r.mailbox.toLowerCase());check(identities.length===1,'sender-identity-ambiguous');
        await mailVerifyComposer(tab,r,identities[0].id);await messenger.tabs.remove(tab.id);
      }
    }
    const result={status:'ok',draft:{message:h,folder_id:h.folder.id,internet_message_id:h.internet_message_id,raw_sha256:raw},
      attachment_bytes_verified:true,reconciled:true,draft_command_reissued:false,binding_matches:h.folder.id===r.draft_folder_id};
    await messenger.storage.local.set({[key]:{...old,stage:'complete',result}});return result;
  }
  if(r.action==='send-verify') {
    if(r.reconcile===true) {
      const key='pilot:send-message:'+r.approval_id,old=(await messenger.storage.local.get(key))[key];check(old,'prior-send-journal-missing');
      const original=JSON.parse(old.fingerprint);
      check(original.high_importance===r.high_importance,'prior-send-priority-changed');
      if(original.reply_internet_message_id)r={...r,reply_internet_message_id:original.reply_internet_message_id};
    }
    const scan=await mailScan(r,a,true);if(!scan.complete)return {status:'blocked',code:'send-verification-incomplete',errors:scan.errors};
    const exact=scan.rows.filter(v=>v.body_match && v.envelope_match && v.attachments_match && v.reply_match && v.priority_match);
    const sent=exact.filter(v=>(v.header.folder.specialUse || []).includes('sent') || v.header.folder.id===r.sent_folder_id);
    const logical=new Set(sent.map(v=>v.header.internet_message_id));
    const pending=exact.filter(v=>(v.header.folder.specialUse || []).some(k=>['drafts','outbox'].includes(k)));
    let delivered=false,deliveryMessage=null;
    if(r.inbox_folder_id && [...(r.to || []),...(r.cc || []),...(r.bcc || [])].every(v=>v.toLowerCase()===r.mailbox.toLowerCase())) {
      const now=new Date(),received=await query({...r,query:undefined,start:r.start || new Date(now.getTime()-30*86400000).toISOString(),end:now.toISOString()},r.inbox_folder_id,logical.size===1 ? [...logical][0] : undefined);
      const matches=[];
      if(received.complete)for(const h of received.messages) {
        const value=await mailMatches(r,h,true);
        if(value && value.body_match && value.envelope_match && value.attachments_match && value.reply_match)matches.push(value.header);
      }
      if(matches.length===1){delivered=true;deliveryMessage=matches[0];}
    }
    const verified=logical.size===1 && pending.length===0;
    if((verified || (delivered && pending.length===0)) && r.reconcile===true) {
      const key='pilot:send-message:'+r.approval_id,old=(await messenger.storage.local.get(key))[key];
      check(old,'prior-send-journal-missing');
      const original=JSON.parse(old.fingerprint);
      for(const k of ['account_id','mailbox','to','cc','bcc','subject','body','attachments']) {
        const one=k==='attachments' ? (original[k] || []).map(v=>({name:v.name,size:v.size,sha256:v.sha256})) : original[k];
        const two=k==='attachments' ? (r[k] || []).map(v=>({name:v.name,size:v.size,sha256:v.sha256})) : r[k];
        check(fingerprint(one)===fingerprint(two),'prior-send-request-changed');
      }
      await messenger.storage.local.set({[key]:{...old,stage:'complete',result:{status:'ok',sent_verified:verified,reconciled:true,send_command_reissued:false,delivery_verified:delivered}}});
    }
    return {status:'ok',sent_verified:logical.size===1 && pending.length===0,delivery_verified:delivered,delivery_message:deliveryMessage,sent_copy_count:sent.length,
      logical_sent_count:logical.size,pending_count:pending.length,messages:sent.map(v=>v.header),complete:true,
      candidates:scan.rows.map(v=>({message_id:v.header.id,folder_id:v.header.folder.id,body_match:v.body_match,envelope_match:v.envelope_match,attachments_match:v.attachments_match,reply_match:v.reply_match,priority_match:v.priority_match}))};
  }
  if(['draft-message','draft-update','send-message'].includes(r.action))return mailCompose(r,a);
  if(r.action==='read-extended') {
    const h=await message(r),full=await messenger.messages.getFull(h.id),after=await message(r);check(h.read===after.read,'read-state-changed');
    return {status:'ok',message:header(after),full,body:await mailText(full),exact_subject:(await messenger.messengerUtilities.decodeMimeHeader('subject',full.headers?.subject || [after.subject]))[0] || after.subject,attachments:await mailAttachmentInfo(h.id),
      ...(r.include_raw_hash ? {raw_sha256:await mailRawHash(h.id)} : {}),read_state_preserved:true};
  }
  if(r.action==='contacts') {
    check(Array.isArray(r.address_book_ids) && r.address_book_ids.length>0,'verified-address-books-required');
    const contacts=[];let scanned=0,complete=true;
    for(const id of r.address_book_ids) {
      const book=await messenger.addressBooks.get(id,true);let seen=0;
      for(const person of book.contacts || []) {
        if(++seen>(r.max_items_per_folder || 200)){complete=false;break;}scanned++;
        const p=person.properties || {},emails=[p.PrimaryEmail,p.SecondEmail].filter(Boolean);
        if([p.DisplayName,p.FirstName,p.LastName,...emails].join('\n').toLowerCase().includes(r.query.toLowerCase()))contacts.push({id:person.id,book_id:id,name:p.DisplayName || [p.FirstName,p.LastName].filter(Boolean).join(' '),emails});
      }
    }
    if(r.include_recipient_cache) {
      const end=new Date(),start=new Date(end.getTime()-30*86400000);
      for(const folderId of r.transport_folder_ids || []) {
        const f=await folder(r,folderId);if(!(f.specialUse || []).includes('sent') && folderId!==r.sent_folder_id)continue;
        const page=await query({...r,query:undefined,subject:undefined,start:start.toISOString(),end:end.toISOString(),max_scan:r.max_items_per_folder || 200},folderId);complete=complete && page.complete;
        for(const h of page.messages)for(const email of await mailAddresses([...h.recipients,...(h.ccList || [])]))if(email.includes(r.query.toLowerCase()))contacts.push({id:h.headerMessageId,book_id:'recipient-cache',name:'',emails:[email]});
      }
    }
    return {status:'ok',contacts:contacts.slice(0,r.limit || 25),complete:complete && contacts.length<=(r.limit || 25),scanned,coverage:'configured-client-address-books'};
  }
  if(r.action==='client-refresh') {
    check(!!messenger.harnessMail,'sync-helper-not-installed');return messenger.harnessMail.execute({...r,action:'sync-folders'});
  }
  return null;
}
