/* Optional Experiment API: only explicit calendar IDs and bounded operations. */
var {ExtensionCommon} = ChromeUtils.importESModule('resource://gre/modules/ExtensionCommon.sys.mjs');
var {cal} = ChromeUtils.importESModule('resource:///modules/calendar/calUtils.sys.mjs');
var {CalIcsParser} = ChromeUtils.importESModule('resource:///modules/CalIcsParser.sys.mjs');

function requireCalendar(condition, code) { if(!condition)throw new Error(code); }
function calendarInfo(c) {
  return {id:c.id,name:c.name,type:c.type,read_only:c.readOnly,disabled:!!c.getProperty('disabled'),
    uri:c.uri.spec,identity:c.getProperty('imip.identity')?.email || null};
}
function calendarDate(d) {
  if(d.isDate)return d.icalString.replace(/^(\d{4})(\d{2})(\d{2})$/,'$1-$2-$3');
  return new Date(d.nativeTime/1000).toISOString();
}
function calendarEvent(e) {
  return {id:e.id,recurrence_id:e.recurrenceId?.icalString || null,title:e.title,
    start:calendarDate(e.startDate),end:calendarDate(e.endDate),all_day:e.startDate.isDate,
    location:e.getProperty('LOCATION') || '',icalendar:e.icalString};
}
function recurrenceSignature(e) {
  return JSON.stringify(e.recurrenceInfo ? e.recurrenceInfo.getRecurrenceItems().map(r=>r.icalString).sort() : []);
}
function parseEvent(text) {
  const p=new CalIcsParser();p.parseString(text);
  const items=p.getItems();
  requireCalendar(items.length===1 && !p.getParentlessItems().length,'one-master-event-required');
  const e=items[0];
  requireCalendar(e.isEvent() && e.id && !e.recurrenceId,'master-event-required');
  requireCalendar(!e.getAttendees().length && !e.organizer,'invitations-not-supported');
  requireCalendar(e.startDate && e.endDate && e.startDate.compare(e.endDate)<0,'invalid-event-range');
  return e;
}
var harnessCalendar=class extends ExtensionCommon.ExtensionAPI {
  getAPI(context) {
    return {harnessCalendar:{execute:async r=>{try {
      if(r.action==='calendar-list')return {status:'ok',calendars:cal.manager.getCalendars().map(calendarInfo)};
      const c=cal.manager.getCalendarById(r.calendar_id);
      requireCalendar(c && !c.getProperty('disabled'),'configured-calendar-unavailable');
      if(r.action==='calendar-query') {
        const start=cal.createDateTime(),end=cal.createDateTime();
        start.nativeTime=new Date(r.start).getTime()*1000;end.nativeTime=new Date(r.end).getTime()*1000;
        requireCalendar(Number.isFinite(start.nativeTime) && start.compare(end)<0,'bounded-calendar-window-required');
        const limit=r.limit || 300;
        requireCalendar(Number.isInteger(limit) && limit>0 && limit<=5000,'invalid-calendar-limit');
        const events=await cal.iterate.streamToArray(c.getItems(
          Ci.calICalendar.ITEM_FILTER_TYPE_EVENT | Ci.calICalendar.ITEM_FILTER_CLASS_OCCURRENCES,
          limit+1,start,end));
        events.sort((a,b)=>a.startDate.compare(b.startDate) || a.id.localeCompare(b.id));
        return {status:'ok',events:events.slice(0,limit).map(calendarEvent),complete:events.length<=limit,
          calendar:calendarInfo(c),coverage:'bounded-client-calendar',server_verified:false};
      }
      requireCalendar(typeof r.event_id==='string' && r.event_id.length>0,'exact-event-id-required');
      const before=await c.getItem(r.event_id);
      if(r.action==='calendar-read')return {status:'ok',event:before ? calendarEvent(before) : null,server_verified:false};
      requireCalendar(!c.readOnly && !Services.io.offline,'calendar-read-only-or-offline');
      if(r.action==='calendar-create') {
        requireCalendar(!before,'event-id-already-exists');
        const e=parseEvent(r.icalendar);requireCalendar(e.id===r.event_id,'event-id-mismatch');
        await c.addItem(e);
      } else if(r.action==='calendar-update' || r.action==='calendar-delete') {
        requireCalendar(before && before.icalString===r.expected_icalendar,'event-changed-since-approval');
        requireCalendar(!before.getAttendees().length && !before.organizer,'invitations-not-supported');
        if(r.action==='calendar-update') {
          const e=parseEvent(r.icalendar);requireCalendar(e.id===r.event_id,'event-id-mismatch');
          await c.modifyItem(e,before);
        } else await c.deleteItem(before);
      } else throw new Error('unsupported-calendar-action');
      const after=await c.getItem(r.event_id);
      requireCalendar(r.action==='calendar-delete' ? !after : !!after,'calendar-write-not-verified');
      if(after) {
        const wanted=parseEvent(r.icalendar);
        requireCalendar(after.title===wanted.title && after.startDate.compare(wanted.startDate)===0 &&
          after.endDate.compare(wanted.endDate)===0 && after.startDate.isDate===wanted.startDate.isDate &&
          recurrenceSignature(after)===recurrenceSignature(wanted), 'calendar-readback-mismatch');
        for(const property of ['DESCRIPTION','LOCATION','URL','STATUS','TRANSP','CLASS','PRIORITY']) {
          const expected=wanted.getProperty(property);
          // Providers assign defaults such as STATUS:CONFIRMED. Explicit values
          // and content clearing still require exact readback.
          if(expected!==null || ['DESCRIPTION','LOCATION','URL'].includes(property)) {
            requireCalendar(String(after.getProperty(property) ?? '')===String(expected ?? ''),'calendar-property-readback-mismatch');
          }
        }
      }
      return {status:'ok',event:after ? calendarEvent(after) : null,client_verified:true,server_verified:false};
    } catch(error) {
      return {status:'blocked',code:/^[a-z][a-z0-9-]+$/.test(error.message) ? error.message : 'calendar-provider-error',
        mutation_may_have_occurred:['calendar-create','calendar-update','calendar-delete'].includes(r.action)};
    }}}};
  }
};
