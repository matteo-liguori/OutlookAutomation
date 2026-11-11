/* Mutations are limited to newly created, uniquely marked self-test objects. */
function testSubject(request, kind) {
    if (!/^[a-f0-9]{12}$/.test(request.token)) reject('invalid-test-token', 'validation');
    return '[OutlookAutomation test ' + request.token + '] ' + kind;
}

function testPreflight(app, request) {
    selectedInbox(app, request);
    try { bindAccount(app, request.mailbox); }
    catch (error) { if (error.safeCode !== 'target-account-not-scriptable') throw error; }
}

function ownedMailFolder(app, request, folderId) {
    var inbox = selectedInbox(app, request);
    var rootId = String(inbox.container().id());
    var folder = app.mailFolders.byId(Number(folderId));
    if (String(folder.container().id()) !== rootId) reject('test-folder-not-in-gmail-root', 'identity');
    return folder;
}

function testCalendarEvent(app, request) {
    var calendar = namedCalendar(app, request);
    var found = calendar.calendarEvents.whose({id:Number(request.object_id)})();
    if (found.length !== 1) reject('test-event-not-found', 'identity');
    var event = found[0], subject = event.subject(), expected = testSubject(request, 'calendar');
    if (String(event.calendar().id()) !== request.calendar_id || (subject !== expected && subject !== expected + ' updated')) {
        reject('not-the-created-test-event', 'identity');
    }
    return event;
}

function testDraft(app, request) {
    var folder = ownedMailFolder(app, request, request.draft_folder_id);
    var found = folder.outgoingMessages.whose({id:Number(request.object_id)})();
    if (found.length !== 1 || found[0].subject() !== testSubject(request, 'self-mail')) reject('not-the-created-test-draft', 'identity');
    return found[0];
}

function selfRecipients(message, mailbox) {
    var recipients = message.toRecipients();
    return recipients.length === 1 && sameMailbox(recipients[0].emailAddress().address, mailbox)
        && message.ccRecipients().length === 0 && message.bccRecipients().length === 0;
}

function nativeTestAction(request, app) {
    var subject = testSubject(request, request.action.indexOf('calendar-') === 0 ? 'calendar' : 'self-mail');
    var phase = 'identity';
    try {
        if (request.authorized !== true) reject('test-authorization-required', 'validation');
        testPreflight(app, request);
        var result = {status:'ok', action:request.action, token:request.token, transport:'outlook-apple-events'};
        phase = request.action;
        if (request.action === 'calendar-create') {
            var calendar = namedCalendar(app, request);
            var start = new Date(request.start), end = new Date(request.end);
            if (!isFinite(start.getTime()) || !isFinite(end.getTime()) || end <= start || end-start > (request.all_day ? 27*3600000 : 30*60000)) reject('invalid-test-event-times', 'validation');
            if (calendar.calendarEvents.whose({subject:subject})().length) reject('test-token-already-exists', 'identity');
            var properties={subject:subject, startTime:start, endTime:end,
                plainTextContent:'Synthetic native Outlook connectivity test. Test ID: ' + request.token,
                allDayFlag:request.all_day===true, isPrivate:true};
            if(request.timezone_name)properties.timezone={name:request.timezone_name};
            if(request.daily_count) {
                if(request.daily_count%1!==0 || request.daily_count<2 || request.daily_count>5)reject('invalid-test-recurrence','validation');
                properties.recurrence={startDate:start,endDate:{endType:'end numbered type',data:request.daily_count},recurrenceType:'daily',occurrenceInterval:1};
            }
            calendar.calendarEvents.push(app.CalendarEvent(properties));
            var created = calendar.calendarEvents.whose({subject:subject})();
            if (created.length !== 1) reject('created-event-not-verified', phase);
            result.id = String(created[0].id());
            result.start = iso(created[0].startTime()); result.end = iso(created[0].endTime());
            result.subject = created[0].subject();
            result.recurring=created[0].isRecurring();result.all_day=created[0].allDayFlag();
        } else if(request.action==='calendar-import') {
            var importCalendar=namedCalendar(app,request);
            if(importCalendar.calendarEvents.whose({subject:subject})().length)reject('test-token-already-exists','identity');
            app.importIcs(Path(request.ics_path),{to:importCalendar});
            var imported=importCalendar.calendarEvents.whose({subject:subject})();
            result.ids=imported.map(function(event){return String(event.id());});
            result.recurring=imported.length ? imported[0].isRecurring() : null;
            if(!imported.length)reject('imported-fixture-not-verified',phase);
        } else if (request.action==='calendar-find') {
            var found=namedCalendar(app,request).calendarEvents.whose({_or:[{subject:subject},{subject:subject+' updated'}]})();
            result.items=found.map(function(event){return {id:String(event.id()),subject:event.subject()};});
        } else if (request.action === 'calendar-update') {
            var event = testCalendarEvent(app, request);
            var newEnd = new Date(request.end);
            if (!isFinite(newEnd.getTime()) || newEnd <= event.startTime() || newEnd-event.startTime() > 30*60000) reject('invalid-test-event-times', 'validation');
            event.subject = subject + ' updated'; event.endTime = newEnd;
            result.id = String(event.id()); result.subject = event.subject(); result.end = iso(event.endTime());
            if (result.subject !== subject + ' updated' || result.end !== newEnd.toISOString()) reject('event-update-not-verified', phase);
        } else if (request.action === 'calendar-delete') {
            var eventToDelete = testCalendarEvent(app, request);
            app.delete(eventToDelete);
            result.removed = namedCalendar(app, request).calendarEvents.whose({id:Number(request.object_id)})().length === 0;
            if (!result.removed) reject('event-delete-not-verified', phase);
        } else if (request.action === 'mail-create') {
            var drafts = ownedMailFolder(app, request, request.draft_folder_id);
            if (drafts.messages.whose({subject:subject})().length) reject('test-token-already-exists', 'identity');
            var draft = app.OutgoingMessage({subject:subject, plainTextContent:request.body, sender:{address:request.mailbox}});
            drafts.outgoingMessages.push(draft);
            var createdDrafts = drafts.outgoingMessages.whose({subject:subject})();
            if (createdDrafts.length !== 1) reject('created-draft-not-verified', phase);
            var createdDraft = createdDrafts[0];
            createdDraft.toRecipients.push(app.ToRecipient({emailAddress:{address:request.mailbox}}));
            result.id = String(createdDraft.id()); result.subject = createdDraft.subject();
            result.sender_verified = sameMailbox(createdDraft.sender().address, request.mailbox);
            result.recipients_verified = selfRecipients(createdDraft, request.mailbox);
        } else if (request.action === 'mail-send') {
            var toSend = testDraft(app, request);
            if (!sameMailbox(toSend.sender().address, request.mailbox) || !selfRecipients(toSend, request.mailbox)
                || toSend.plainTextContent() !== request.body) reject('self-mail-envelope-or-body-mismatch', 'identity');
            app.send(toSend);
            result.submitted = true; result.delivery_verified = false;
        } else if (request.action === 'mail-find') {
            result.items = [];
            for (var f=0; f<request.mail_folder_ids.length; f++) {
                var mailFolder = ownedMailFolder(app, request, request.mail_folder_ids[f]);
                var messages = mailFolder.messages.whose({subject:subject})();
                for (var m=0; m<messages.length; m++) {
                    var message=messages[m], before=message.isRead();
                    var sender=message.sender();
                    if (!sameMailbox(sender.address, request.mailbox)) reject('test-message-sender-mismatch', 'identity');
                    var body=message.plainTextContent();
                    if (before !== message.isRead()) reject('read-state-changed', 'read-state');
                    result.items.push({id:String(message.id()), folder_id:String(mailFolder.id()), subject:message.subject(),
                        is_read:before, body:body, sender:sender.address, received:iso(message.timeReceived())});
                }
            }
        } else if (request.action === 'mail-delete') {
            var sourceFolder = ownedMailFolder(app, request, request.object_folder_id);
            var matched = sourceFolder.messages.whose({id:Number(request.object_id)})();
            if (!matched.length) { result.removed=true; result.already_absent=true; return result; }
            if (matched.length !== 1 || matched[0].subject() !== subject || !sameMailbox(matched[0].sender().address, request.mailbox)) reject('not-the-created-test-message', 'identity');
            app.delete(matched[0]);
            result.removed = sourceFolder.messages.whose({id:Number(request.object_id)})().length === 0;
            if (!result.removed) reject('message-delete-not-verified', phase);
        } else {
            reject('unsupported-test-action', 'validation');
        }
        selectedInbox(app, request);
        return result;
    } catch (error) {
        return {status:'blocked', action:request.action, code:error.safeCode || 'native-test-action-failed',
            phase:error.phase || phase, native_error_number:typeof error.errorNumber==='number' ? error.errorNumber : null,
            token:request.token, mutation_may_have_occurred:phase!=='identity'};
    }
}

function run(argv) {
    return JSON.stringify(nativeTestAction(JSON.parse(argv[0]), Application('com.microsoft.Outlook')));
}
