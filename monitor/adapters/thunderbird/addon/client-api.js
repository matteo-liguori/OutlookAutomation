/* Narrow privileged mail helpers: exact draft reopening and IMAP sync callbacks. */
var {ExtensionCommon}=ChromeUtils.importESModule('resource://gre/modules/ExtensionCommon.sys.mjs');
var {setTimeout,clearTimeout}=ChromeUtils.importESModule('resource://gre/modules/Timer.sys.mjs');
var {MailServices}=ChromeUtils.importESModule('resource:///modules/MailServices.sys.mjs');
var {getFolder}=ChromeUtils.importESModule('resource:///modules/ExtensionAccounts.sys.mjs');
function requireMail(value,code) {if(!value)throw new Error(code);}
var harnessMail=class extends ExtensionCommon.ExtensionAPI {
  getAPI(context) {
    return {harnessMail:{execute:async r=>{try {
      const account=MailServices.accounts.getAccount(r.account_id);
      requireMail(account && [...account.identities].some(i=>i.email.toLowerCase()===r.mailbox.toLowerCase()),'native-account-identity-mismatch');
      if(r.action==='sync-folders') {
        requireMail(!Services.io.offline,'client-offline');
        requireMail(Array.isArray(r.folder_ids) && r.folder_ids.length>0 && r.folder_ids.length<=8,'bounded-sync-folders-required');
        const results=[];
        for(const id of r.folder_ids) {
          const target=getFolder(id);requireMail(target.accountKey===r.account_id && target.folder.server.type==='imap','sync-folder-account-or-type-mismatch');
          const result=await new Promise(resolve=>{
            let complete=false;const done=value=>{if(!complete){complete=true;clearTimeout(timer);resolve(value);}};
            const timer=setTimeout(()=>done({folder_id:id,success:false,code:'sync-callback-timeout'}),Math.min(r.timeout_ms || 8000,15000));
            const listener={QueryInterface:ChromeUtils.generateQI(['nsIUrlListener']),OnStartRunningUrl(){},
              OnStopRunningUrl(url,status){done({folder_id:id,success:Components.isSuccessCode(status),status_code:status,completed_at:new Date().toISOString()});}};
            try {target.folder.QueryInterface(Ci.nsIMsgImapMailFolder).updateFolderWithListener(null,listener);}
            catch(error){done({folder_id:id,success:false,code:'sync-call-failed'});}
          });
          results.push(result);
        }
        return {status:'ok',success:results.every(v=>v.success),folders:results,provider_callback_verified:results.every(v=>v.success),completed_at:new Date().toISOString()};
      }
      if(['hold-composer','discard-composer','inspect-composer'].includes(r.action)) {
        const win=context.extension.tabManager.get(r.tab_id).nativeTab;
        requireMail(win.gMsgCompose && [...account.identities].some(i=>i.key===win.gCurrentIdentity.key && i.email.toLowerCase()===r.mailbox.toLowerCase()),'owned-composer-sender-mismatch');
        if(r.action==='inspect-composer')return {status:'ok',draft_folder:win.gCurrentIdentity.getUnicharAttribute('draft_folder'),transport:win.__outlookAutomationTransport || [],configured_draft_folder:r.draft_folder_id ? getFolder(r.draft_folder_id).folder.URI : null,
          sent_copy_uri:win.gMsgCompose.compFields.fcc,additional_copy_uri:win.gMsgCompose.compFields.fcc2,draft_uri:win.gMsgCompose.compFields.draftId,original_uri:win.gMsgCompose.originalMsgURI,type:win.gComposeType,saving:!!win.gSaveOperationInProgress,sending:!!win.gSendOperationInProgress};
        requireMail(!win.gSendOperationInProgress && !win.gSaveOperationInProgress,'composer-transport-in-progress');
        win.clearTimeout(win.gAutoSaveTimeout);win.gAutoSaveTimeout=null;
        if(r.action==='hold-composer') {
          requireMail(r.draft_folder_id,'verified-draft-folder-required');
          const target=getFolder(r.draft_folder_id);
          requireMail(target.accountKey===r.account_id && target.folder.canFileMessages,'draft-folder-not-writable');
          requireMail(win.gCurrentIdentity.getUnicharAttribute('draft_folder')===target.folder.URI,'client-draft-folder-binding-mismatch');
          const sent=getFolder(r.sent_folder_id);
          requireMail(sent.accountKey===r.account_id && sent.folder.canFileMessages && win.gMsgCompose.compFields.fcc===sent.folder.URI && !win.gMsgCompose.compFields.fcc2,'client-sent-folder-binding-mismatch');
          if(!win.__outlookAutomationTransport) {
            const events=win.__outlookAutomationTransport=[];
            const record=(event,status,detail)=>{events.push({event,status,detail:String(detail || '').slice(0,500)});if(events.length>8)events.shift();};
            win.gMsgCompose.addMsgSendListener({onStartSending(){},onProgress(){},onStatus(id,text){record('status',null,text);},
              onStopSending(id,status){record('sending-stopped',status);},onGetDraftFolderURI(id,uri){record('saved-folder',null,uri);},
              onSendNotPerformed(id,status){record('not-performed',status);},onTransportSecurityError(){record('transport-security-error',null);}});
          }
        }
        if(r.action==='discard-composer') {
          win.gContentChanged=false;win.gMsgCompose.bodyModified=false;win.gAutoSaveKickedIn=false;win.gReceiptOptionChanged=false;win.gDSNOptionChanged=false;
          win.close();
        }
        return {status:'ok',automatic_save_paused:true,discarded:r.action==='discard-composer',saved_message_deleted:false};
      }
      if(r.action==='open-draft' || r.action==='draft-tab') {
        const h=context.extension.messageManager.get(r.message_id);
        requireMail(h && h.folder.server.key===account.incomingServer.key && h.folder.getFlag(Ci.nsMsgFolderFlags.Drafts),'exact-draft-account-required');
        requireMail(h.messageId===r.internet_message_id || !r.internet_message_id,'draft-rfc-id-changed');
        const uri=h.folder.getUriForMsg(h);
        if(r.action==='draft-tab') {
          const tabs=[],diagnostics=[];
          for(const win of Services.wm.getEnumerator('msgcompose')) {
            if(win.gMsgCompose?.compFields?.subject===h.mime2DecodedSubject && win.gCurrentIdentity?.email.toLowerCase()===r.mailbox.toLowerCase())diagnostics.push({tab_id:context.extension.tabManager.convert(win).id,original_uri:win.gMsgCompose?.originalMsgURI,draft_uri:win.gMsgCompose?.compFields?.draftId,expected_uri:uri,type:win.gComposeType,ready:win.composeEditorReady});
            if(win.gMsgCompose?.originalMsgURI===uri || win.gMsgCompose?.compFields?.draftId?.split('?')[0]===uri)tabs.push(context.extension.tabManager.convert(win));
          }
          return {status:'ok',tabs,diagnostics};
        }
        for(const win of Services.wm.getEnumerator('msgcompose')) {
          requireMail(win.gMsgCompose?.originalMsgURI!==uri && win.gMsgCompose?.compFields?.draftId?.split('?')[0]!==uri,'draft-already-open-for-editing');
        }
        const identities=[...account.identities].filter(i=>i.email.toLowerCase()===r.mailbox.toLowerCase());requireMail(identities.length===1,'native-sender-identity-ambiguous');
        // Let Thunderbird's MIME converter load the real draft, including its
        // existing Draft-ID and reply headers, just as its native edit command.
        const previous=new Set(Services.wm.getEnumerator('msgcompose'));
        MailServices.compose.OpenComposeWindow(null,h,uri,Ci.nsIMsgCompType.Draft,
          identities[0].composeHtml ? Ci.nsIMsgCompFormat.OppositeOfDefault : Ci.nsIMsgCompFormat.Default,identities[0],null,null);
        const win=await new Promise((resolve,reject)=>{
          const started=Date.now();
          const inspect=()=>{
            for(const candidate of Services.wm.getEnumerator('msgcompose'))if(!previous.has(candidate) && candidate.composeEditorReady &&
              (candidate.gMsgCompose?.originalMsgURI===uri || candidate.gMsgCompose?.compFields?.draftId?.split('?')[0]===uri)) {resolve(candidate);return;}
            if(Date.now()-started>=15000){reject(new Error('draft-editor-timeout'));return;}
            setTimeout(inspect,100);
          };inspect();
        });
        requireMail(win.gMsgCompose.originalMsgURI===uri || win.gMsgCompose.compFields.draftId?.split('?')[0]===uri,'draft-editor-identity-changed');
        win.clearTimeout(win.gAutoSaveTimeout);win.gAutoSaveTimeout=null;
        return {status:'ok',tab:context.extension.tabManager.convert(win)};
      }
      throw new Error('unsupported-native-mail-action');
    } catch(error) {
      return {status:'blocked',code:/^[a-z][a-z0-9-]+$/.test(error.message) ? error.message : 'native-client-mail-error',mutation_may_have_occurred:false};
    }}}};
  }
};
