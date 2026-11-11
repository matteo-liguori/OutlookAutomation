/* Run with Node. Synthetic MailExtension APIs, no client or network access. */
const vm=require('node:vm'),assert=require('node:assert/strict');
const {TextEncoder}=require('node:util');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto').webcrypto,{File}=require('node:buffer');
async function run(source) {
  function fixture() {
    const data={send:0,save:0,open:0,closed:0,reply:0,details:null,files:[],records:new Map(),storage:{}};
    const root={id:'account1://',accountId:'account1',subFolders:['Sent','Drafts'].map(name=>({id:'account1://'+name,accountId:'account1',specialUse:[name.toLowerCase()]}))};
    const folder=name=>({id:'account1://'+name,accountId:'account1',path:'/'+name,specialUse:[name.toLowerCase()]});
    const header=(id,name)=>({id,headerMessageId:'fixture-'+id+'@example.invalid',author:'fixture@example.invalid',recipients:['fixture@example.invalid'],ccList:[],bccList:[],subject:data.details.subject.replace(/^Re: /,''),priority:data.details.priority,read:name!=='INBOX',date:new Date(),folder:folder(name)});
    const messenger={runtime:{connectNative:()=>({onMessage:{addListener(){}},onDisconnect:{addListener(){}},postMessage(){}})},
      storage:{local:{get:async key=>({[key]:data.storage[key]}),set:async values=>Object.assign(data.storage,values)}},
      accounts:{get:async()=>({id:'account1',identities:[{id:'id1',email:'fixture@example.invalid'}],rootFolder:root}),list:async()=>[]},
      folders:{get:async id=>folder(id.split('//')[1])},messengerUtilities:{parseMailboxString:async text=>[{email:text}],decodeMimeHeader:async(name,value)=>value,convertToPlainText:async text=>text.replace(/<[^>]*>/g,'').trim()},
      messages:{get:async id=>structuredClone(data.records.get(id)),
        query:async q=>({messages:[...data.records.values()].filter(h=>(!q.folderId || h.folder.id===q.folderId) && (!q.headerMessageId || h.headerMessageId===q.headerMessageId) && (!q.subject || h.subject.toLowerCase().includes(q.subject.toLowerCase()))).map(h=>structuredClone(h))}),
        getFull:async()=>({contentType:data.html ? 'text/html':'text/plain',body:data.html ? '<pre>'+data.details.plainTextBody+'</pre>':data.details.plainTextBody,headers:{subject:[data.details.subject],...(data.reply ? {'in-reply-to':['<fixture-31@example.invalid>']} : {})}}),
        getRaw:async()=>new File([JSON.stringify({details:data.details,files:data.files.map(v=>v.name)})],'message.eml'),
        listAttachments:async()=>data.files.map((file,i)=>({name:file.name,partName:'1.'+(i+2),size:file.size,contentType:file.type})),
        getAttachmentFile:async(id,part)=>data.files[Number(part.split('.')[1])-2]},
      compose:{beginNew:async details=>{data.details=details;data.files=[];return {id:1};},
        beginReply:async(id,type,details)=>{data.reply=id;data.details=details;data.files=[];return {id:1};},
        setComposeDetails:async(id,details)=>{data.details=details;},getComposeDetails:async()=>data.details,
        listAttachments:async()=>data.files.map((file,i)=>({id:i+1,name:file.name})),
        addAttachment:async(id,{file})=>{data.files.push(file);},removeAttachment:async(id,n)=>{data.files.splice(n-1,1);},
        getAttachmentFile:async id=>data.files[id-1],
        saveMessage:async()=>{data.save++;data.records.clear();const h=header(20+data.save,'Drafts');data.records.set(h.id,h);return {messages:[data.staleCallback ? {...h,headerMessageId:'stale-callback',read:!h.read}:h]};},
        sendMessage:async()=>{data.send++;data.records.clear();const sent=header(31,'Sent'),inbox=header(32,'INBOX');inbox.headerMessageId=sent.headerMessageId;data.records.set(31,sent);data.records.set(32,inbox);return {messages:[sent],headerMessageId:sent.headerMessageId};}},
      tabs:{remove:async()=>{data.closed++;}},
      harnessMail:{execute:async r=>{if(r.action==='hold-composer')return {status:'ok'};if(r.action==='discard-composer')return {status:'ok',discarded:true,saved_message_deleted:false};if(r.action==='draft-tab')return {status:'ok',tabs:[]};data.open++;return {status:'ok',tab:{id:1}};}},
      addressBooks:{get:async()=>({contacts:[{id:'self',properties:{DisplayName:'Fixture',PrimaryEmail:'fixture@example.invalid'}}]})}}
    const context=vm.createContext({messenger,console,structuredClone,crypto,File,TextEncoder,btoa,atob,setTimeout,clearTimeout});new vm.Script(source).runInContext(context);
    return {context,data};
  }
  const base={account_id:'account1',mailbox:'fixture@example.invalid'};
  const bytes=Uint8Array.from({length:200003},(_,i)=>i%256),sha=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(v=>v.toString(16).padStart(2,'0')).join('');
  const spec={name:'café.bin',size:bytes.length,sha256:sha,upload_id:'a'.repeat(32),content_type:'application/octet-stream'};
  const envelope={...base,approval_id:'one',to:['fixture@example.invalid'],cc:[],bcc:[],subject:'Synthetic',body:'Exact body',high_importance:true,attachments:[spec],draft_folder_id:'account1://Drafts',sent_folder_id:'account1://Sent',inbox_folder_id:'account1://INBOX',transport_folder_ids:['account1://Drafts','account1://Sent']};
  const f=fixture();f.data.staleCallback=true;f.data.html=true;const dispatch=r=>f.context.dispatch(r);
  for(let offset=0;offset<bytes.length;offset+=192*1024) {
    const chunk=bytes.slice(offset,offset+192*1024);let text='';for(const b of chunk)text+=String.fromCharCode(b);
    await dispatch({...base,action:'attachment-upload',upload_id:spec.upload_id,offset,total_size:bytes.length,data:btoa(text)});
  }
  const saved=await dispatch({...envelope,action:'draft-message'});assert.equal(saved.attachment_bytes_verified,true);assert.equal(f.data.save,1);
  assert.equal((await dispatch({...envelope,action:'draft-message'})).duplicate_prevented,true);assert.equal(f.data.save,1);
  const reconciled=await dispatch({...envelope,action:'draft-reconcile',original_action:'draft-message'});assert.equal(reconciled.draft_command_reissued,false);assert.equal(f.data.save,1);
  await assert.rejects(dispatch({...envelope,action:'draft-update',approval_id:'update',message_id:21,source_folder_id:'account1://Drafts',internet_message_id:saved.draft.internet_message_id,expected_raw_sha256:'0'.repeat(64)}),/changed-since-approval/);assert.equal(f.data.open,0);
  const updated=await dispatch({...envelope,action:'draft-update',approval_id:'update',body:'Updated body',message_id:21,source_folder_id:'account1://Drafts',internet_message_id:saved.draft.internet_message_id,expected_raw_sha256:saved.draft.raw_sha256});
  assert.equal(f.data.open,1);assert.equal(f.data.save,2);assert.notEqual(updated.draft.raw_sha256,saved.draft.raw_sha256);
  const send={...envelope,action:'send-message',approval_id:'update',body:'Updated body'};
  await dispatch(send);assert.equal(f.data.send,1);assert.equal(f.data.open,2);
  assert.equal((await dispatch({...send,attachments:[{...spec,upload_id:'different-ephemeral-id'}]})).duplicate_prevented,true);assert.equal(f.data.send,1);
  const verified=await dispatch({...send,action:'send-verify'});assert.equal(verified.sent_verified,true);assert.equal(verified.delivery_verified,true);
  await assert.rejects(dispatch({...send,approval_id:'another'}),/related-transport-message-exists/);assert.equal(f.data.send,1);
  const opened=await dispatch({...base,action:'attachment-open',message_id:32,part_name:'1.2'});assert.equal(opened.sha256,sha);
  const gathered=[];for(let offset=0;offset<bytes.length;offset+=192*1024){const result=await dispatch({...base,action:'attachment-chunk',message_id:32,download_id:opened.download_id,offset});for(const character of atob(result.data))gathered.push(character.charCodeAt(0));}
  assert.deepEqual(Uint8Array.from(gathered),bytes);
  const contacts=await dispatch({...base,action:'contacts',query:'fixture',address_book_ids:['book1']});assert.equal(contacts.contacts[0].emails[0],'fixture@example.invalid');
  const reply={...envelope,action:'draft-message',approval_id:'reply',subject:'Re: Synthetic',reply_message_id:32,reply_folder_id:'account1://INBOX',reply_internet_message_id:'fixture-31@example.invalid'};
  await dispatch(reply);assert.equal(f.data.reply,32);
  // A failed pre-save composer stays owned and can only be discarded after
  // an exact snapshot; a possible save must instead use read-only reconciliation.
  const failed=fixture();failed.data.details={};
  const failedRequest={...envelope,attachments:[],action:'draft-message',approval_id:'failed'};
  failed.context.messenger.compose.getComposeDetails=async()=>({...failed.data.details,plainTextBody:'changed'});
  await assert.rejects(failed.context.dispatch(failedRequest),/composer-body-changed/);
  const inspected=await failed.context.dispatch({...base,action:'draft-inspect',original_action:'draft-message',approval_id:'failed'});
  await assert.rejects(failed.context.dispatch({...base,action:'draft-discard',original_action:'draft-message',approval_id:'failed',compose_hash:'0'.repeat(64)}),/changed-since-inspection/);
  const discarded=await failed.context.dispatch({...base,action:'draft-discard',original_action:'draft-message',approval_id:'failed',compose_hash:inspected.compose_hash});
  assert.equal(discarded.discarded,true);assert.equal(failed.data.save,0);assert.equal(failed.data.send,0);
  const interrupted=fixture();interrupted.context.messenger.compose.saveMessage=async()=>{throw new Error('lost-save-response');};
  await assert.rejects(interrupted.context.dispatch({...failedRequest,approval_id:'saving'}),/lost-save-response/);
  const pending=await interrupted.context.dispatch({...base,action:'draft-inspect',original_action:'draft-message',approval_id:'saving'});
  const canceled=await interrupted.context.dispatch({...base,action:'draft-discard',original_action:'draft-message',approval_id:'saving',compose_hash:pending.compose_hash});assert.equal(canceled.saved_message_deleted,false);assert.equal(interrupted.data.send,0);
  const wrongCopy=fixture();wrongCopy.context.messenger.compose.getComposeDetails=async()=>({...wrongCopy.data.details,overrideDefaultFccFolder:{id:'account1://Wrong'}});
  await assert.rejects(wrongCopy.context.dispatch({...failedRequest,approval_id:'wrong-copy'}),/sent-copy-folder-changed/);assert.equal(wrongCopy.data.save,0);assert.equal(wrongCopy.data.send,0);
  return 'Passed byte-verified multi-frame attachment transfer, draft save/update/send, changed-version refusal, safe replies, contact lookup, duplicate preflight and independent Sent/self-Inbox verification.';
}
module.exports=run;
if(require.main===module)run(['mail.js','background.js'].map(name=>fs.readFileSync(path.join(__dirname,'../monitor/adapters/thunderbird/addon',name),'utf8')).join('\n')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
