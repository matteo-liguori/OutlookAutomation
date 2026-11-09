/* Read-only Apple-event adapter. No UI, shell, credential or write operations. */
function reject(code, phase) {
    var error = new Error(code);
    error.safeCode = code;
    error.phase = phase;
    throw error;
}

function sameMailbox(value, expected) {
    return typeof value === 'string' && value.toLowerCase() === expected.toLowerCase();
}

function ownerIsTarget(object, expected) {
    if (!sameMailbox(object.account().emailAddress(), expected)) {
        reject('owner-not-target', 'identity');
    }
}

function bindAccount(app, mailbox) {
    var matches = [];
    var types = ['imapAccounts', 'exchangeAccounts', 'popAccounts'];
    for (var t = 0; t < types.length; t++) {
        var accounts = app[types[t]]();
        for (var i = 0; i < accounts.length; i++) {
            if (!sameMailbox(accounts[i].emailAddress(), mailbox)) {
                reject('other-account-present', 'identity');
            }
            matches.push({account: accounts[i], type: types[t]});
        }
    }
    if (matches.length === 0) reject('target-account-not-scriptable', 'identity');
    if (matches.length !== 1) reject('ambiguous-target-account', 'identity');
    return matches[0];
}

function iso(value) {
    var date = new Date(value);
    if (!isFinite(date.getTime())) reject('invalid-native-date', 'read');
    return date.toISOString();
}

function selectedInbox(app, request) {
    if (app.mainWindows[0].name() !== 'Inbox • ' + request.mailbox) reject('window-identity-changed', 'identity');
    var folder = app.selectedFolder();
    if (folder.name() !== 'Inbox' || String(folder.id()) !== request.folder_id) reject('selected-inbox-changed', 'identity');
    return folder;
}

function mailboxFolder(app, request, id) {
    var anchor = selectedInbox(app, request), root = String(anchor.container().id());
    var folder = app.mailFolders.byId(Number(id)), node = folder, nodes = [];
    for (var depth=0; depth<32; depth++) {
        if (String(node.id())===root) return {folder:folder,path:request.mailbox+'/'+nodes.reverse().map(function(part){return part.name();}).join('/')};
        nodes.push(node);
        node=node.container();
    }
    reject('folder-not-in-confirmed-mailbox','identity');
}

function mailboxFolders(app, request) {
    var anchor=selectedInbox(app,request),root=String(anchor.container().id()),rows=[];
    var folders=app.mailFolders();
    for(var i=0;i<folders.length;i++) {
        var node=folders[i],owned=false;
        try { for(var depth=0;depth<32;depth++) { if(String(node.id())===root){owned=true;break;}node=node.container(); } } catch(error){}
        if(!owned || String(folders[i].id())===root)continue;
        var bound=mailboxFolder(app,request,String(folders[i].id())),count=null;
        try {count=folders[i].messages().length;}catch(error){}
        rows.push({id:String(folders[i].id()),name:folders[i].name(),path:bound.path,items:count});
    }
    selectedInbox(app,request);return rows;
}

function namedCalendar(app, request) {
    var title = app.mainWindows[0].name();
    if (title.slice(-(' • ' + request.mailbox).length) !== ' • ' + request.mailbox) reject('window-identity-changed', 'identity');
    var calendar = app.calendars.byId(Number(request.calendar_id));
    if (!sameMailbox(calendar.name(), request.mailbox) || String(calendar.id()) !== request.calendar_id) reject('calendar-identity-mismatch', 'identity');
    var owner = null;
    try { owner = calendar.account().emailAddress(); } catch (error) {}
    if (owner && !sameMailbox(owner, request.mailbox)) reject('other-calendar-account-present', 'identity');
    return calendar;
}

function calendarInventory(app, request) {
    var calendars = app.calendars(), rows = [];
    for (var i = 0; i < calendars.length; i++) {
        var owner = null, container = null;
        try { owner = calendars[i].account().emailAddress(); } catch (error) {}
        if (owner && !sameMailbox(owner, request.mailbox)) reject('other-calendar-account-present', 'identity');
        try { container = calendars[i].container().name(); } catch (error) {}
        rows.push({id:String(calendars[i].id()), name:calendars[i].name(), owner:owner, container:container,
            matches_mailbox_name:sameMailbox(calendars[i].name(), request.mailbox)});
    }
    return rows;
}

function nativeCollect(request, app) {
    var phase = 'identity';
    try {
        if (request.command === 'discover') {
            if (app.mainWindows[0].name() !== 'Inbox • ' + request.mailbox || app.selectedFolder().name() !== 'Inbox') reject('select-verified-mailbox-inbox', 'identity');
            request = JSON.parse(JSON.stringify(request));
            request.binding = 'selected-inbox';
            request.folder_id = String(app.selectedFolder().id());
        }
        var selectedBinding = request.binding === 'selected-inbox';
        var calendarBinding = request.binding === 'named-calendar';
        var bound;
        if (selectedBinding || calendarBinding) {
            // Google inboxes can expose native folders/messages while omitting
            // account objects. Caller must first verify this inbox in Outlook UI.
            if (selectedBinding) selectedInbox(app, request);
            else namedCalendar(app, request);
            try {
                bound = bindAccount(app, request.mailbox);
            } catch (identityError) {
                if (identityError.safeCode !== 'target-account-not-scriptable') throw identityError;
                bound = {type: selectedBinding ? 'ui-verified-selected-inbox' : 'mailbox-named-calendar', account: null};
            }
        } else {
            bound = bindAccount(app, request.mailbox);
        }
        var result = {
            status: 'ok', transport: 'outlook-apple-events', mailbox: request.mailbox,
            account_type: bound.type, command: request.command, writes_performed: false
        };
        if (selectedBinding) result.binding = {kind:'selected-inbox', folder_id:request.folder_id, window_title:'Inbox • ' + request.mailbox};
        if (calendarBinding) result.binding = {kind:'named-calendar', calendar_id:request.calendar_id, name:request.mailbox};
        if (request.command === 'check') {
            result.content_read = false;
            return result;
        }
        if (request.command === 'discover') {
            result.inbox_id = request.folder_id;
            result.mail_root_id = String(selectedInbox(app, request).container().id());
            result.calendars = calendarInventory(app, request);
            result.content_read = false;
            selectedInbox(app, request);
            return result;
        }
        if (request.command === 'calendars') {
            result.calendars = calendarInventory(app, request);
            result.content_read = false;
            return result;
        }
        if(request.command==='folders') {
            if(!selectedBinding)reject('folder-discovery-requires-selected-inbox','identity');
            result.folders=mailboxFolders(app,request);result.content_read=false;return result;
        }
        phase = 'query';
        var directMessage=request.command==='message';
        if(directMessage && !/^[0-9]+$/.test(request.entry_id||''))reject('invalid-entry-id','validation');
        var start = directMessage ? null : new Date(request.start), end = directMessage ? null : new Date(request.end);
        if (!directMessage && (!isFinite(start.getTime()) || !isFinite(end.getTime()) || start >= end)) {
            reject('invalid-date-window', phase);
        }
        if (end - start > (request.command==='calendar'?31:7) * 86400000+(request.capture?1:0)) reject('date-window-too-wide', phase);
        if (request.limit < 1 || request.limit > (request.capture || request.command==='calendar' ? 5000 : 100) || request.limit % 1 !== 0) reject('invalid-limit', phase);
        if(!directMessage)result.window = {start: start.toISOString(), end_exclusive: end.toISOString()};
        result.items = [];
        result.truncated = false;
        var account = bound.account;
        if (request.command === 'mail' || directMessage) {
            if (calendarBinding) reject('mail-requires-inbox-binding', 'identity');
            var folderBinding = selectedBinding && request.target_folder_id ? mailboxFolder(app,request,request.target_folder_id) : null;
            var inbox = folderBinding ? folderBinding.folder : selectedBinding ? selectedInbox(app, request) : account.inbox();
            var sourceFolderId = String(inbox.id());
            result.folder_path = folderBinding ? folderBinding.path : request.mailbox+'/Inbox';
            if (!selectedBinding) ownerIsTarget(inbox, request.mailbox);
            var dateClause = {_and: [
                {timeReceived: {_greaterThanEquals: start}},
                {timeReceived: {_lessThan: end}}
            ]};
            var filter = directMessage ? {id:Number(request.entry_id)} : request.subject === undefined ? dateClause : {_and: [dateClause, {subject: request.subject}]};
            if (!directMessage && request.entry_id !== undefined) filter = {_and:[filter, {id:Number(request.entry_id)}]};
            if (request.unread_only) filter = {_and:[filter, {isRead:false}]};
            var messages = inbox.messages.whose(filter)();
            var initialMailIds = request.capture ? messages.map(function(message){return String(message.id());}).sort() : null;
            result.scope = folderBinding ? 'target-account-folder' : 'target-account-inbox';
            result.filter = request.subject === undefined ? {} : {subject_exact: request.subject};
            if (request.entry_id !== undefined) result.filter.entry_id = request.entry_id;
            if (request.unread_only) result.filter.unread_only = true;
            result.matched_count = messages.length;
            result.truncated = messages.length > request.limit;
            messages.sort(function(a,b){return new Date(b.timeReceived()) - new Date(a.timeReceived());});
            var queryTotal = messages.length;
            messages = messages.slice(0, request.search_query ? request.max_scan : request.limit);
            var before = [];
            // Validate every owner before reading any subject or body.
            for (var m = 0; m < messages.length; m++) {
                if (selectedBinding) {
                    if (String(messages[m].folder().id()) !== sourceFolderId) reject('message-folder-mismatch', 'identity');
                } else {
                    ownerIsTarget(messages[m], request.mailbox);
                }
                before.push(messages[m].isRead());
            }
            phase = 'read';
            for (var j = 0; j < messages.length; j++) {
                var message = messages[j];
                var received = iso(message.timeReceived());
                if (!directMessage && (new Date(received) < start || new Date(received) >= end)) reject('native-filter-outside-window', phase);
                var item = {id: String(message.id()), folder_id:sourceFolderId, folder_path:result.folder_path, subject: message.subject(), received_at: received, is_read: before[j]};
                if (request.entry_id !== undefined && item.id !== request.entry_id) reject('native-id-filter-mismatch', phase);
                if (request.unread_only && item.is_read !== false) reject('native-unread-filter-mismatch', phase);
                var sender = message.sender();
                item.from = sender.name || '';
                item.sender_email = sender.address || '';
                item.message_class = 'outlook-macos:message';
                item.internet_message_id = ''; item.conversation_id = '';
                if (request.capture || request.include_body) {
                    try {
                        var headers = message.headers().replace(/\r?\n[ \t]+/g,' ');
                        var internetId = /^Message-ID:\s*(.+)$/mi.exec(headers);
                        item.internet_message_id = internetId ? internetId[1].trim() : '';
                    } catch (headerError) {}
                    try { item.conversation_id = String(message.localThreadGuid()); } catch (threadError) {}
                    try { if (message.isMeeting()) item.message_class='outlook-macos:meeting-message'; } catch (classError) {}
                }
                if (request.subject !== undefined && item.subject !== request.subject) reject('native-subject-filter-mismatch', phase);
                var text = null;
                if(request.search_query) {
                    text=message.plainTextContent();
                    var haystack=[item.subject,item.from,item.sender_email,text].join('\n').toLowerCase();
                    if(haystack.indexOf(request.search_query.toLowerCase())<0)continue;
                }
                if (request.include_body) {
                    item.plain_text = text===null ? message.plainTextContent() : text;
                    item.to = message.toRecipients().map(function(recipient){return recipient.emailAddress().address;});
                    item.cc = message.ccRecipients().map(function(recipient){return recipient.emailAddress().address;});
                    item.attachment_names = message.attachments().map(function(attachment){return attachment.name();});
                    item.attachment_info = message.attachments().map(function(attachment,index){var size=null;try{size=attachment.fileSize();}catch(error){}return {index:index+1,name:attachment.name(),size:size};});
                }
                result.items.push(item);
                if(request.search_query && result.items.length>=request.limit)break;
            }
            for (var k = 0; k < messages.length; k++) {
                if (messages[k].isRead() !== before[k]) reject('read-state-changed', 'read-state');
            }
            if (selectedBinding) selectedInbox(app, request);
            result.read_state_preserved = true;
            if(request.search_query) {
                result.scanned=Math.min(j+1,messages.length);result.matched_count=result.items.length;
                result.truncated=result.scanned<queryTotal;result.filter.query=request.search_query;
            }
            if (request.capture) {
                var finalMailIds = inbox.messages.whose(filter)().map(function(message){return String(message.id());}).sort();
                result.capture_complete = !result.truncated && JSON.stringify(initialMailIds)===JSON.stringify(finalMailIds);
                result.errors = [];
                if (result.truncated) result.errors.push('Bounded snapshot scan cap reached');
                if (JSON.stringify(initialMailIds)!==JSON.stringify(finalMailIds)) result.errors.push('Inbox membership changed during capture');
            }
        } else if (request.command === 'calendar') {
            if (selectedBinding) reject('calendar-requires-calendar-binding', 'identity');
            var calendars = calendarBinding ? [namedCalendar(app, request)] : account.calendars();
            if (calendars.length === 0) reject('no-calendars-exposed-for-target-account', phase);
            if (request.expand_recurrence) {
                result.calendar_sources = []; result.errors = []; result.calendar_scan_complete = true;
                for (var ci = 0; ci < calendars.length; ci++) {
                    if (!calendarBinding) ownerIsTarget(calendars[ci], request.mailbox);
                    var records = calendars[ci].calendarEvents();
                    var initialIds = records.map(function(record){return String(record.id());}).sort();
                    if (records.length > request.max_scan) {
                        result.calendar_scan_complete = false; result.errors.push('Calendar scan cap reached');
                    }
                    var selected = records.slice(0, request.max_scan);
                    for (var ri = 0; ri < selected.length; ri++) {
                        if (String(selected[ri].calendar().id()) !== String(calendars[ci].id())) reject('event-calendar-mismatch','identity');
                        try {
                            result.calendar_sources.push({id:String(selected[ri].id()),calendar_id:String(calendars[ci].id()),all_day:selected[ri].allDayFlag(),icalendar:selected[ri].icalendarData()});
                        } catch (exportError) {
                            result.errors.push('Native calendar export failed for ID '+String(selected[ri].id()));
                            result.calendar_scan_complete = false;
                        }
                    }
                    var finalIds = calendars[ci].calendarEvents().map(function(record){return String(record.id());}).sort();
                    if (JSON.stringify(initialIds)!==JSON.stringify(finalIds)) {
                        result.errors.push('Calendar membership changed during native export'); result.calendar_scan_complete = false;
                    }
                }
                if (calendarBinding) namedCalendar(app,request);
                result.scope='calendars-owned-by-target-account';
                return result;
            }
            var all = [];
            for (var c = 0; c < calendars.length; c++) {
                if (!calendarBinding) ownerIsTarget(calendars[c], request.mailbox);
                var events = calendars[c].calendarEvents.whose({_and: [
                    {startTime: {_lessThan: end}}, {endTime: {_greaterThan: start}}
                ]})();
                for (var e = 0; e < events.length; e++) all.push({event: events[e], calendar: calendars[c]});
            }
            result.scope = 'calendars-owned-by-target-account';
            result.matched_count = all.length;
            result.truncated = all.length > request.limit;
            all.sort(function(a,b){return new Date(a.event.startTime()) - new Date(b.event.startTime());});
            all = all.slice(0, request.limit);
            for (var a = 0; a < all.length; a++) {
                if (calendarBinding) {
                    if (String(all[a].event.calendar().id()) !== request.calendar_id) reject('event-calendar-mismatch', 'identity');
                } else ownerIsTarget(all[a].event, request.mailbox);
            }
            phase = 'read';
            for (var n = 0; n < all.length; n++) {
                var event = all[n].event;
                var eventStart = iso(event.startTime()), eventEnd = iso(event.endTime());
                if (new Date(eventStart) >= end || new Date(eventEnd) <= start) reject('native-filter-outside-window', phase);
                var entry = {id: String(event.id()), calendar_id: String(all[n].calendar.id()), subject: event.subject(),
                    start: eventStart, end: eventEnd, location: event.location(), all_day: event.allDayFlag(), recurring: event.isRecurring()};
                if (request.include_body) entry.plain_text = event.plainTextContent();
                result.items.push(entry);
            }
            if (calendarBinding) namedCalendar(app, request);
            result.recurrence_coverage = 'native-query-results-only; occurrence expansion not established';
        } else {
            reject('unknown-command', 'validation');
        }
        result.items.sort(function(a, b) {
            var order = request.command === 'mail' || directMessage ? new Date(b.received_at)-new Date(a.received_at) : new Date(a.start)-new Date(b.start);
            return order || String(a.id).localeCompare(String(b.id));
        });
        result.coverage = result.truncated ? 'partial-limit' : 'bounded-native-query';
        return result;
    } catch (error) {
        // Native exception text can contain private account/object descriptions.
        return {status: 'blocked', code: error.safeCode || 'native-command-failed',
            phase: error.phase || phase, native_error_number: typeof error.errorNumber === 'number' ? error.errorNumber : null,
            transport: 'outlook-apple-events', writes_performed: false};
    }
}

function run(argv) {
    var request = JSON.parse(argv[0]);
    return JSON.stringify(nativeCollect(request, Application('com.microsoft.Outlook')));
}
