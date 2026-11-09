/* Native operations invoked only after the Python broker validates authority. */
function approvedPreflight(app,request) {
    if(request.authorized!==true)reject('authorization-required','validation');
    selectedInbox(app,request);
    try {bindAccount(app,request.mailbox);}catch(error){if(error.safeCode!=='target-account-not-scriptable')throw error;}
}
function addresses(message,field) {
    return message[field]().map(function(recipient){return recipient.emailAddress().address.toLowerCase();}).sort();
}
function verifyEnvelope(message,request) {
    var expected=request.message;
    if(!sameMailbox(message.sender().address,request.mailbox) || message.subject()!==expected.subject || message.plainTextContent()!==expected.body)reject('approved-message-changed','identity');
    ['to','cc','bcc'].forEach(function(field){
        if(JSON.stringify(addresses(message,field+'Recipients'))!==JSON.stringify(expected[field].map(function(address){return address.toLowerCase();}).sort()))reject('approved-recipients-changed','identity');
    });
    var attachments=message.attachments();
    if(attachments.length!==expected.attachments.length)reject('approved-attachments-changed','identity');
    // Outlook fileSize can be MIME-encoded size. Python verifies exported bytes.
    for(var i=0;i<attachments.length;i++)if(attachments[i].name()!==expected.attachments[i].name)reject('approved-attachments-changed','identity');
}
function operationDraft(app,request) {
    var folder=mailboxFolder(app,request,request.draft_folder_id).folder;
    var found=folder.outgoingMessages.whose({id:Number(request.object_id)})();
    if(found.length!==1 || String(found[0].folder().id())!==request.draft_folder_id)reject('exact-draft-not-found','identity');
    return found[0];
}
function messageInFolder(app,request) {
    var folder=mailboxFolder(app,request,request.source_folder_id).folder;
    var found=folder.messages.whose({id:Number(request.object_id)})();
    if(found.length!==1 || String(found[0].folder().id())!==request.source_folder_id)reject('exact-message-not-found','identity');
    return found[0];
}
function messageInternetId(message) {
    var match=/^Message-ID:\s*(.+)$/mi.exec(message.headers().replace(/\r?\n[ \t]+/g,' '));
    return match ? match[1].trim() : '';
}
function nativeOperation(request,app) {
    var phase='identity';
    try {
        approvedPreflight(app,request);
        var result={status:'ok',action:request.action,transport:'outlook-apple-events'};
        if(request.action==='draft-create') {
            var drafts=mailboxFolder(app,request,request.draft_folder_id).folder;
            if(!request.allow_duplicate) {
                var related=mailboxFolders(app,request).filter(function(folder){return ['drafts','sent','sent mail','sent items','outbox'].indexOf(folder.name.toLowerCase())>=0;});
                for(var rf=0;rf<related.length;rf++)if(mailboxFolder(app,request,related[rf].id).folder.messages.whose({subject:request.message.subject})().length)reject('related-mail-exists','identity');
            }
            phase='create';
            drafts.outgoingMessages.push(app.OutgoingMessage({subject:request.message.subject,plainTextContent:request.message.body,sender:{address:request.mailbox}}));
            var candidates=drafts.outgoingMessages.whose({subject:request.message.subject})();
            if(candidates.length!==1)reject('draft-creation-ambiguous','create');
            var created=candidates[0];result.id=String(created.id());
            ['to','cc','bcc'].forEach(function(field){
                var className={to:'ToRecipient',cc:'CcRecipient',bcc:'BccRecipient'}[field];
                request.message[field].forEach(function(address){created[field+'Recipients'].push(app[className]({emailAddress:{address:address}}));});
            });
            request.message.attachments.forEach(function(attachment){created.attachments.push(app.Attachment({file:Path(attachment.path)}));});
            verifyEnvelope(created,request);
            if(request.display)app.open(created);
            result.displayed=request.display===true;result.Status='draft-saved';
        } else if(request.action==='draft-update') {
            var draft=operationDraft(app,request);phase='update';
            draft.subject=request.message.subject;draft.plainTextContent=request.message.body;
            ['to','cc','bcc'].forEach(function(field){
                var current=addresses(draft,field+'Recipients'),wanted=request.message[field].map(function(address){return address.toLowerCase();}).sort();
                if(JSON.stringify(current)!==JSON.stringify(wanted)) {
                    draft[field+'Recipients']().forEach(function(recipient){app.delete(recipient);});
                    var className={to:'ToRecipient',cc:'CcRecipient',bcc:'BccRecipient'}[field];
                    request.message[field].forEach(function(address){draft[field+'Recipients'].push(app[className]({emailAddress:{address:address}}));});
                }
            });
            verifyEnvelope(draft,request);result.id=String(draft.id());result.Status='draft-updated';
        } else if(request.action==='draft-send') {
            var toSend=operationDraft(app,request);verifyEnvelope(toSend,request);
            if(request.expected_modification && iso(toSend.modificationDate())!==request.expected_modification)reject('draft-modified-after-verification','identity');
            phase='send-attempt';app.send(toSend);result.id=request.object_id;result.Status='submitted';result.delivery_verified=false;
        } else if(request.action==='attachment-save') {
            var message=messageInFolder(app,request),before=message.isRead();
            var attachments=message.attachments();
            if(request.attachment_index<1 || request.attachment_index>attachments.length)reject('attachment-index-out-of-range','validation');
            var attachment=attachments[request.attachment_index-1];
            if(attachment.name()!==request.attachment_name)reject('attachment-changed','identity');
            phase='attachment-save';app.save(attachment,{in:Path(request.destination)});
            if(before!==message.isRead())reject('read-state-changed','read-state');
            result.saved=true;result.read_state_preserved=true;
        } else if(request.action==='message-move') {
            var source=messageInFolder(app,request),before=source.isRead();
            if(messageInternetId(source)!==request.internet_message_id || !request.internet_message_id)reject('message-identity-mismatch','identity');
            var destination=mailboxFolder(app,request,request.destination_folder_id).folder;
            var subject=source.subject();phase='move';app.move(source,{to:destination});
            var moved=destination.messages.whose({subject:subject})().filter(function(message){return messageInternetId(message)===request.internet_message_id;});
            if(moved.length!==1)reject('move-not-verified','move');
            result.id=String(moved[0].id());result.folder_id=String(destination.id());
            result.read_state_restored=moved[0].isRead()!==before;
            if(result.read_state_restored)moved[0].isRead=before;
            result.read_state_preserved=moved[0].isRead()===before;
            if(!result.read_state_preserved)reject('move-read-state-changed','read-state');
            if(request.source_folder_id!==request.destination_folder_id && mailboxFolder(app,request,request.source_folder_id).folder.messages.whose({id:Number(request.object_id)})().length)reject('move-source-not-cleared','move');
        } else if(request.action==='message-delete') {
            var target=messageInFolder(app,request);
            if(!request.internet_message_id || messageInternetId(target)!==request.internet_message_id)reject('message-identity-mismatch','identity');
            phase='delete';app.delete(target);
            result.removed=mailboxFolder(app,request,request.source_folder_id).folder.messages.whose({id:Number(request.object_id)})().length===0;
            if(!result.removed)reject('delete-not-verified','delete');
        } else if(request.action==='draft-verify') {
            var inspected=operationDraft(app,request);verifyEnvelope(inspected,request);
            result.modification=iso(inspected.modificationDate());
            result.attachments=inspected.attachments().map(function(attachment,index){return {index:index+1,name:attachment.name(),size:attachment.fileSize()};});
            if(request.display){app.open(inspected);result.displayed=true;}
        } else reject('unknown-native-operation','validation');
        selectedInbox(app,request);return result;
    } catch(error) {
        return {status:'blocked',code:error.safeCode||'native-operation-failed',phase:error.phase||phase,
                id:typeof result==='object'?result.id:undefined,
                native_error_number:typeof error.errorNumber==='number'?error.errorNumber:null,
                mutation_may_have_occurred:['create','update','send-attempt','move','delete'].indexOf(phase)>=0};
    }
}
function run(argv){return JSON.stringify(nativeOperation(JSON.parse(argv[0]),Application('com.microsoft.Outlook')));}
