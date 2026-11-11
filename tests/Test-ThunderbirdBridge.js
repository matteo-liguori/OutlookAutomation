/* Run with Node: node tests/Test-ThunderbirdBridge.js. No client or live mail. */
const vm=require('node:vm'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const crypto=require('node:crypto').webcrypto,{File}=require('node:buffer');

async function run(source) {
  function fixture() {
    const stored={};let moveCount=0,sendCount=0,contentReads=0,listener;
    let h={id:12,headerMessageId:'fixture@example.invalid',subject:'Fixture',date:new Date('2026-11-09T12:00:00Z'),
      author:'Fixture <fixture@example.invalid>',recipients:['fixture@example.invalid'],read:false,
      folder:{id:'account1://INBOX',accountId:'account1'}};
    let moveMode='normal',release,prior;
    const replies=[];
    const messenger={
      runtime:{getBrowserInfo:async()=>({version:'159.0'}),connectNative:()=>({
        onMessage:{addListener:f=>{listener=f;}},onDisconnect:{addListener:()=>{}},postMessage:r=>replies.push(r)})},
      storage:{local:{get:async key=>({[key]:stored[key]}),set:async value=>Object.assign(stored,value)}},
      accounts:{list:async()=>[],get:async()=>({identities:[{id:'id1',email:'fixture@example.invalid'}],rootFolder:{id:'account1://',accountId:'account1',subFolders:[{id:'account1://Sent',accountId:'account1',specialUse:['sent']},{id:'account1://Drafts',accountId:'account1',specialUse:['drafts']}]}})},
      folders:{get:async id=>({id,accountId:'account1'})},
      messages:{get:async()=>structuredClone(h),getFull:async()=>{contentReads++;return {body:'Fixture'};},
        query:async q=>({messages:[...(h.folder.id===q.folderId ? [structuredClone(h)] : []),...(prior && prior.folder.id===q.folderId ? [structuredClone(prior)] : [])]}),
        update:async(id,values)=>Object.assign(h,values),
        move:async(ids,destination)=>{
          moveCount++;
          if(moveMode==='pending')await new Promise(resolve=>{release=resolve;});
          h={...h,id:moveMode==='merged' ? 99 : 13,folder:{id:destination,accountId:'account1'},read:moveMode==='lost-response' || moveMode==='merged' ? false : true};
          if(moveMode==='merged')prior=null;
          if(moveMode==='lost-response')throw new Error('lost-response');
        }},
      messengerUtilities:{decodeMimeHeader:async(name,value)=>value,parseMailboxString:async text=>[{email:text.includes('<') ? text.slice(text.lastIndexOf('<')+1,text.lastIndexOf('>')) : text}]},
      harnessMail:{execute:async()=>({status:'ok'})},
      compose:{listAttachments:async()=>[],beginNew:async details=>{messenger.details=details;return {id:1};},
        getComposeDetails:async()=>messenger.details,
        sendMessage:async()=>{sendCount++;throw new Error('submission-response-lost');}}
    };
    const context=vm.createContext({messenger,console,structuredClone,crypto,File,btoa,atob,setTimeout:(f,ms)=>setTimeout(f,ms===40000?20:ms),clearTimeout});
    new vm.Script(source).runInContext(context);
    return {context,stored,replies,request:r=>listener({id:String(replies.length),request:r}),
      stats:()=>({moveCount,sendCount,contentReads}),addSourceExisting:()=>{prior={...structuredClone(h),id:99};},addExisting:()=>{prior={...structuredClone(h),id:99,read:true,folder:{id:'account1://Trash',accountId:'account1'}};},setMode:value=>{moveMode=value;},release:()=>release()};
  }
  const base={account_id:'account1',mailbox:'fixture@example.invalid'};
  const move={...base,action:'move-message',approval_id:'move',message_id:12,source_folder_id:'account1://INBOX',destination_folder_id:'account1://Filed',internet_message_id:'fixture@example.invalid'};
  let f=fixture();
  await assert.rejects(f.context.dispatch({...base,mailbox:'foreign@example.invalid',action:'read',message_id:12}),/identity-mismatch/);
  assert.equal(f.stats().contentReads,0);
  await assert.rejects(f.context.dispatch({...base,action:'read',message_id:12,internet_message_id:'wrong'}),/changed/);
  assert.equal(f.stats().contentReads,0);
  const first=await f.context.dispatch(move);assert.equal(first.read_state_preserved,true);assert.equal(first.message.read,false);
  const reordered=Object.fromEntries(Object.entries(move).reverse());
  assert.equal((await f.context.dispatch(reordered)).duplicate_prevented,true);assert.equal(f.stats().moveCount,1);
  await assert.rejects(f.context.dispatch({...move,destination_folder_id:'account1://Another'}),/changed/);
  f=fixture();f.addExisting();
  const trash={...move,action:'trash-message',approval_id:'trash',destination_folder_id:'account1://Trash'};
  const trashed=await f.context.dispatch(trash);assert.equal(trashed.recoverable,true);assert.equal(trashed.message.read,false);assert.equal(f.stats().moveCount,1);
  assert.equal((await f.context.dispatch(trash)).duplicate_prevented,true);
  f=fixture();f.addSourceExisting();const duplicateSource=await f.context.dispatch(trash);assert.equal(duplicateSource.recoverable,true);assert.equal(f.stats().moveCount,1);
  f=fixture();f.addExisting();f.setMode('merged');const merged=await f.context.dispatch(trash);
  assert.equal(merged.destination_merged,true);assert.equal(merged.read_state_preserved,true);
  f=fixture();f.addExisting();f.setMode('merged');const otherRead={...trash,approval_id:'different-read'};
  // Simulate a read Sent view merging into an existing unread recovery copy.
  const rawGet=f.context.messenger.messages.get;f.context.messenger.messages.get=async id=>({...await rawGet(id),read:true});
  const different=await f.context.dispatch(otherRead);assert.equal(different.read_state_preserved,false);assert.equal(different.recoverable,true);assert.equal(different.warnings.length,1);
  f=fixture();f.setMode('lost-response');
  await assert.rejects(f.context.dispatch(move),/lost-response/);
  await assert.rejects(f.context.dispatch(move),/reconciliation/);
  const reconciled=await f.context.dispatch({...move,action:'reconcile-move'});
  assert.equal(reconciled.reconciled,true);assert.equal(reconciled.move_command_reissued,false);assert.equal(f.stats().moveCount,1);
  f=fixture();const send={...base,action:'send-message',approval_id:'send',to:['fixture@example.invalid'],cc:[],bcc:[],subject:'Fixture',body:'Synthetic',sent_folder_id:'account1://Sent',draft_folder_id:'account1://Drafts'};
  await assert.rejects(f.context.dispatch(send),/lost/);await assert.rejects(f.context.dispatch(send),/reconciliation/);assert.equal(f.stats().sendCount,1);
  f=fixture();f.setMode('pending');f.request(move);
  await new Promise(resolve=>setTimeout(resolve,40));
  assert.equal(f.replies[0].result.code,'client-operation-timeout');
  f.request({action:'status'});await new Promise(resolve=>setTimeout(resolve,5));
  assert.equal(f.replies[1].result.status,'ok');assert.equal(f.replies[1].result.write_busy,true);
  f.request({...move,approval_id:'second'});assert.equal(f.replies[2].result.code,'client-write-in-progress');
  f.release();await new Promise(resolve=>setTimeout(resolve,10));assert.equal(f.stats().moveCount,1);
  return 'Passed account isolation, changed identity, unread restoration, canonical replay, uncertain-send suppression, read-only reconciliation and responsive reads during stalled writes.';
}
module.exports=run;
if(require.main===module)run(fs.readFileSync(path.join(__dirname,'../outlook_platforms/thunderbird/addon/mail.js'),'utf8')+'\n'+fs.readFileSync(path.join(__dirname,'../outlook_platforms/thunderbird/addon/background.js'),'utf8')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
