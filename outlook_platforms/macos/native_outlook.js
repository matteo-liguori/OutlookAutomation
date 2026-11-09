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
        if (request.command === 'calendars') {
            result.calendars = calendarInventory(app, request);
            result.content_read = false;
            return result;
        }
        phase = 'query';
        var start = new Date(request.start), end = new Date(request.end);
        if (!isFinite(start.getTime()) || !isFinite(end.getTime()) || start >= end) {
            reject('invalid-date-window', phase);
        }
        if (end - start > 7 * 86400000) reject('date-window-exceeds-seven-days', phase);
        if (request.limit < 1 || request.limit > 100 || request.limit % 1 !== 0) reject('invalid-limit', phase);
        result.window = {start: start.toISOString(), end_exclusive: end.toISOString()};
        result.items = [];
        result.truncated = false;
        var account = bound.account;
        if (request.command === 'mail') {
            if (calendarBinding) reject('mail-requires-inbox-binding', 'identity');
            var inbox = selectedBinding ? selectedInbox(app, request) : account.inbox();
            if (!selectedBinding) ownerIsTarget(inbox, request.mailbox);
            var dateClause = {_and: [
                {timeReceived: {_greaterThanEquals: start}},
                {timeReceived: {_lessThan: end}}
            ]};
            var filter = request.subject === undefined ? dateClause : {_and: [dateClause, {subject: request.subject}]};
            if (request.entry_id !== undefined) filter = {_and:[filter, {id:Number(request.entry_id)}]};
            if (request.unread_only) filter = {_and:[filter, {isRead:false}]};
            var messages = inbox.messages.whose(filter)();
            result.scope = 'target-account-inbox';
            result.filter = request.subject === undefined ? {} : {subject_exact: request.subject};
            if (request.entry_id !== undefined) result.filter.entry_id = request.entry_id;
            if (request.unread_only) result.filter.unread_only = true;
            result.matched_count = messages.length;
            result.truncated = messages.length > request.limit;
            messages.sort(function(a,b){return new Date(b.timeReceived()) - new Date(a.timeReceived());});
            messages = messages.slice(0, request.limit);
            var before = [];
            // Validate every owner before reading any subject or body.
            for (var m = 0; m < messages.length; m++) {
                if (selectedBinding) {
                    if (String(messages[m].folder().id()) !== request.folder_id) reject('message-folder-mismatch', 'identity');
                } else {
                    ownerIsTarget(messages[m], request.mailbox);
                }
                before.push(messages[m].isRead());
            }
            phase = 'read';
            for (var j = 0; j < messages.length; j++) {
                var message = messages[j];
                var received = iso(message.timeReceived());
                if (new Date(received) < start || new Date(received) >= end) reject('native-filter-outside-window', phase);
                var item = {id: String(message.id()), subject: message.subject(), received_at: received, is_read: before[j]};
                if (request.entry_id !== undefined && item.id !== request.entry_id) reject('native-id-filter-mismatch', phase);
                if (request.unread_only && item.is_read !== false) reject('native-unread-filter-mismatch', phase);
                var sender = message.sender();
                item.from = sender.name || '';
                item.sender_email = sender.address || '';
                if (request.subject !== undefined && item.subject !== request.subject) reject('native-subject-filter-mismatch', phase);
                if (request.include_body) {
                    item.plain_text = message.plainTextContent();
                    item.to = message.toRecipients().map(function(recipient){return recipient.emailAddress().address;});
                    item.cc = message.ccRecipients().map(function(recipient){return recipient.emailAddress().address;});
                    item.attachment_names = message.attachments().map(function(attachment){return attachment.name();});
                }
                result.items.push(item);
            }
            for (var k = 0; k < messages.length; k++) {
                if (messages[k].isRead() !== before[k]) reject('read-state-changed', 'read-state');
            }
            if (selectedBinding) selectedInbox(app, request);
            result.read_state_preserved = true;
        } else if (request.command === 'calendar') {
            if (selectedBinding) reject('calendar-requires-calendar-binding', 'identity');
            var calendars = calendarBinding ? [namedCalendar(app, request)] : account.calendars();
            if (calendars.length === 0) reject('no-calendars-exposed-for-target-account', phase);
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
            var order = request.command === 'mail' ? new Date(b.received_at)-new Date(a.received_at) : new Date(a.start)-new Date(b.start);
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
