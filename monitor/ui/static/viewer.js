'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const params = new URLSearchParams(location.hash.slice(1));
  const tokenKey = 'outlook-viewer-token';
  if (params.has('token')) {
    sessionStorage.setItem(tokenKey, params.get('token'));
    params.delete('token');
    history.replaceState(null, '', location.pathname + (params.size ? '#' + params : ''));
  }
  const token = sessionStorage.getItem(tokenKey) || '';
  const labels = {'needs-user':'Needs you',waiting:'Waiting',watch:'Watching',deadline:'Deadline',promise:'Promised',
    'follow-up':'Follow-up',review:'Review','snooze-end':'Snooze ends'};
  const dateFormat = new Intl.DateTimeFormat('en-GB', {timeZone:'Europe/London', day:'numeric',month:'short',year:'numeric'});
  const dayFormat = new Intl.DateTimeFormat('en-GB', {timeZone:'Europe/London', weekday:'long'});
  const timeFormat = new Intl.DateTimeFormat('en-GB', {timeZone:'Europe/London',hour:'2-digit',minute:'2-digit'});
  const dayKey = value => new Intl.DateTimeFormat('en-CA', {timeZone:'Europe/London',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value));
  const formatDate = value => dateFormat.format(new Date(value));
  const formatTime = value => timeFormat.format(new Date(value));
  let data = null, mode = 'grid', selected = null, detailRequest = 0, loadRequest = 0, lastRendered = '';
  let dirty = false, saving = false, editAttempt = null;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function dot(colour) {const node = el('span', 'dot'); node.style.backgroundColor = colour; return node;}
  async function api(path, payload) {
    const options = {headers:{'X-Viewer-Token':token}, cache:'no-store', signal:AbortSignal.timeout(10000)};
    if (payload) { options.method='POST'; options.headers['Content-Type']='application/json'; options.body=JSON.stringify(payload); }
    const response = await fetch(path, options);
    if (!response.ok) {
      if (response.status === 401) throw new Error('Open the private link printed by monitor ui to connect this browser.');
      let message = 'Could not load current records.';
      try { message = (await response.json()).error || message; } catch (_) { /* Non-JSON error. */ }
      throw new Error(message);
    }
    return response.json();
  }
  function matchesAction(action) {
    const filter = $('filter').value;
    if (filter === 'all') return true;
    if (filter === 'due') return action.due;
    if (filter === 'snoozed') return action.snoozed;
    return action.status === filter;
  }
  function visibleCards() {
    const query = $('search').value.trim().toLocaleLowerCase();
    return data.cards.filter(card => {
      const haystack = [card.id,card.title,card.purpose,card.position,...card.actions.flatMap(a => [a.next_step,a.owner])].join(' ').toLocaleLowerCase();
      const match = $('filter').value === 'all' || card.actions.some(matchesAction) || ($('filter').value === 'due' && card.review_due_at);
      return match && (!query || haystack.includes(query));
    });
  }
  function openButton(cls, id) {
    const node = el('button', cls); node.type = 'button';
    node.addEventListener('click', () => openDetail(id));
    return node;
  }
  function renderCard(card) {
    const node = openButton('thread-card', card.id);
    node.setAttribute('aria-label', 'Open ' + card.title);
    const strip = el('span', 'colour-strip'); strip.style.backgroundColor = card.colour; node.append(strip);
    const top = el('span', 'card-top'), key = el('span', 'colour-key');
    key.append(dot(card.colour), el('span', '', card.actions.length + (card.actions.length === 1 ? ' open action' : ' open actions')));
    top.append(key);
    if (card.review_due_at) top.append(el('span', 'badge', 'Card review due'));
    else if (card.actions.some(a => a.due)) top.append(el('span', 'badge', 'Needs attention'));
    node.append(top, el('span','thread-title',card.title),el('span','purpose',card.purpose));
    if(card.pending_notes)node.append(el('span','more',card.pending_notes+' '+(card.pending_notes===1?'note':'notes')+' awaiting agent'));
    const preview = el('span','action-preview');
    const actions = card.actions.filter(matchesAction);
    for (const action of actions.slice(0,3)) {
      const line = el('span','action-line');
      line.append(el('span','status-label',(labels[action.status] || action.status) + (action.snoozed ? ' · Snoozed' : '') + '  '),document.createTextNode(action.next_step));
      preview.append(line);
    }
    if (!actions.length) preview.append(el('span','','No matching action. Open the card for context.'));
    if (actions.length > 3) preview.append(el('span','more','+' + (actions.length-3) + ' more · open card'));
    node.append(preview);
    const event = data.events.find(e => e.case_id === card.id && matchesAction(e.action));
    const foot = el('span','card-date');
    if (event) {
      const label = event.kinds.map(k => labels[k]).join(' / ');
      foot.append(el('span','',label),el('span','',formatDate(event.at)));
    } else foot.append(el('span','',card.actions.length ? 'No action date recorded' : 'Context only · no outstanding actions'));
    node.append(foot);
    return node;
  }
  function renderEvent(event, unscheduled = false) {
    const node = openButton('event',event.case_id); node.style.borderLeftColor = event.colour;
    const top = el('div','event-top'), kinds = el('div','');
    if (unscheduled) kinds.append(el('span','kind','No date recorded'));
    else for (const kind of event.kinds) kinds.append(el('span','kind '+kind,labels[kind]));
    top.append(kinds);
    if (!unscheduled) top.append(el('span','',formatTime(event.at)));
    const thread = el('div','event-thread');thread.append(dot(event.colour),el('span','',event.title));
    node.append(top,el('h3','',event.action.next_step),thread,
      el('p','event-bottom',(labels[event.action.status] || event.action.status)+' · '+event.action.owner+(event.action.snoozed?' · Snoozed':'')));
    return node;
  }
  function renderTimeline(cards) {
    const ids = new Set(cards.map(c => c.id)), root = el('div','timeline');
    root.append(el('p','timeline-note','All outstanding dates, in order. An action can appear at several milestones. Midnight may be a date boundary rather than a confirmed cutoff; check the card context.'));
    const groups = new Map();
    for (const event of data.events.filter(e => ids.has(e.case_id) && matchesAction(e.action))) {
      const key = dayKey(event.at); if (!groups.has(key)) groups.set(key, []); groups.get(key).push(event);
    }
    const today = dayKey(data.as_of);
    for (const [day, events] of groups) {
      const section = el('section','day'), heading = el('h2','day-title');
      heading.append(el('strong','',formatDate(events[0].at)),document.createTextNode(day === today ? 'Today' : dayFormat.format(new Date(events[0].at))));
      if (day < today) heading.append(el('span','more','Past date · still open'));
      const list = el('div','day-items');events.forEach(event => list.append(renderEvent(event)));section.append(heading,list);root.append(section);
    }
    const unscheduled = data.unscheduled.filter(e => ids.has(e.case_id) && matchesAction(e.action));
    if (unscheduled.length) {
      const section = el('section','day'), list = el('div','day-items');
      unscheduled.forEach(event => list.append(renderEvent(event,true)));
      section.append(el('h2','day-title','Unscheduled'),list);root.append(section);
    }
    if (!groups.size && !unscheduled.length) root.append(empty('No matching action dates','These cards may contain context without outstanding actions.'));
    return root;
  }
  function empty(title,text) {const node=el('div','empty');node.append(el('h2','',title),el('p','',text));return node;}
  function render() {
    if (!data) return;
    const cards = visibleCards(), actions = cards.flatMap(c => c.actions).filter(matchesAction);
    $('count').textContent = cards.length + ' open ' + (cards.length === 1 ? 'thread' : 'threads') + ' · ' + actions.length + ' matching ' + (actions.length === 1 ? 'action' : 'actions');
    const content = $('content');content.replaceChildren();
    if (!cards.length) content.append(empty(data.cards.length ? 'No matching threads' : 'A clear view. Nothing open.',data.cards.length ? 'Try another search or filter.' : 'Open cards will appear here when they are recorded in the harness.'));
    else if (mode === 'grid') {const grid = el('div','grid');cards.forEach(c => grid.append(renderCard(c)));content.append(grid);}
    else content.append(renderTimeline(cards));
  }
  function section(title,text) {const node=el('section','detail-section');node.append(el('h3','section-label',title));if(text)node.append(el('p','text-block',text));return node;}
  function renderDetail(card) {
    dirty=false;
    $('detail-title').textContent = card.title; $('detail-id').textContent = card.id;
    const body = $('detail-body');body.replaceChildren();
    if(card.user_notes.length){
      const pending=section('Notes awaiting the agent · '+card.user_notes.length);
      for(const note of card.user_notes){const item=el('article','pending-note');item.append(el('p','text-block',note.text),el('p','more','Added '+formatDate(note.created_at)+' · '+formatTime(note.created_at)+' · Pending'));pending.append(item);}
      body.append(pending);
    }
    body.append(editor(card));
    body.append(section('Recognition & goal',card.purpose),section('Current position',card.position));
    const actions = section('Outstanding actions · ' + card.actions.length);
    for (const action of card.actions) {
      const item=el('article','detail-action');item.style.borderLeftColor=card.colour;
      item.append(el('p','',action.next_step),el('div','action-meta',(labels[action.status]||action.status)+' · '+action.owner+(action.snoozed?' · Snoozed':'')));
      const dates=el('dl','date-list');
      for (const [field,label] of [['deadline','Deadline'],['promised_at','Promised'],['follow_up_at','Follow-up'],['next_review','Next review'],['snooze_until','Snoozed until']]) if(action[field])dates.append(el('dt','',label),el('dd','',formatDate(action[field])+' · '+formatTime(action[field])));
      item.append(dates);
      if(action.depends_on.length)item.append(el('p','more','Depends on: '+action.depends_on.join(', ')));
      if(action.completion_condition){const details=el('details','');details.append(el('summary','','Completion condition'),el('p','text-block',action.completion_condition));item.append(details);}
      actions.append(item);
    }
    if(!card.actions.length)actions.append(el('p','text-block','No outstanding actions on this card.'));
    body.append(actions);
    if(card.context)body.append(section('Relevant context',card.context));
    if(card.decisions.length)body.append(section('Current decisions',card.decisions.join('\n\n')));
    if(card.evidence.length){
      const sources=section('Supporting records'),details=el('details',''),list=el('ol','evidence-list');
      details.append(el('summary','',card.evidence.length+' evidence references'));
      for(const evidence of card.evidence){const item=el('li','', (evidence.at?formatDate(evidence.at)+' · ':'')+(evidence.label||'Source'));item.append(el('span','evidence-id',evidence.id));list.append(item);}
      details.append(list);sources.append(details);body.append(sources);
    }
    body.append(el('p','more','Revision '+card.revision+' · Last updated '+formatDate(card.updated_at)+' '+formatTime(card.updated_at)+'. Browsing does not change review clocks.'));
  }
  function editor(card) {
    const root=el('details','editor-panel'), form=el('form','card-editor');
    root.id='edit-panel';root.append(el('summary','','Edit card / leave a note'));
    const fields=[['title','Card name',180,card.title],['purpose','Recognition / goal',600,card.purpose||''],['note','Note for the agent',2000,'']];
    for(const [name,label,limit,value] of fields){
      const wrap=el('label','editor-label',label), input=el(name==='title'?'input':'textarea','');
      input.name=name;input.id='edit-'+name;input.maxLength=limit;input.value=value;
      if(name!=='note')input.required=true;
      if(name!=='title')input.rows=name==='purpose'?3:4;
      if(name==='note')input.placeholder='Anything the agent should know or deal with on its next pass…';
      input.addEventListener('input',()=>{dirty=true;save.disabled=false;status.textContent='Unsaved changes';});
      wrap.append(input);form.append(wrap);
    }
    form.append(el('p','more','Recognition/goal: up to 600 characters and three lines. Notes stay pending until the agent handles them; saving does not send mail or change task dates.'));
    const controls=el('div','editor-controls'), save=el('button','primary','Save changes'), reload=el('button','quiet','Reload card');
    save.type='submit';save.disabled=true;reload.type='button';
    reload.addEventListener('click',()=>{if(!saving && (!dirty || confirm('Discard your unsaved text and load the latest card?'))){dirty=false;openDetail(card.id);}});
    controls.append(save,reload);form.append(controls);
    const status=el('p','editor-status');status.id='edit-status';status.setAttribute('role','status');form.append(status);
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(saving)return;
      const values=new FormData(form), payload={id:card.id,expected_revision:card.revision,title:values.get('title'),purpose:values.get('purpose')};
      if(values.get('note').trim())payload.note=values.get('note');
      if(payload.purpose.split(/\r\n|\r|\n/).length>3){status.textContent='Keep recognition/goal to at most three lines.';return;}
      const fingerprint=JSON.stringify(payload);
      if(!editAttempt || editAttempt.fingerprint!==fingerprint)editAttempt={fingerprint,key:crypto.randomUUID()};
      payload.idempotency_key=editAttempt.key;
      saving=true;save.disabled=true;reload.disabled=true;status.textContent='Saving…';
      // Freeze the form while the request is in flight, without discarding text
      // if the response is lost. A retry uses the same idempotency key.
      for(const input of form.querySelectorAll('input,textarea'))input.disabled=true;
      try {
        await api('/api/card/'+encodeURIComponent(card.id)+'/edit',payload);
        const latest=await api('/api/card/'+encodeURIComponent(card.id));
        dirty=false;editAttempt=null;
        if(selected===card.id){renderDetail(latest);$('edit-panel').open=true;$('edit-status').textContent=payload.note?'Saved. Your note is waiting for the agent.':'Card updated.';}
        await load();
      } catch(error) {status.textContent=error.message+' Nothing in this form has been discarded.';save.disabled=false;}
      finally {saving=false;reload.disabled=false;for(const input of form.querySelectorAll('input,textarea'))input.disabled=false;}
    });
    root.append(form);return root;
  }
  async function openDetail(id) {
    if(saving)return;
    dirty=false;
    selected = id; const sequence = ++detailRequest;
    $('detail-title').textContent='Loading thread…';$('detail-id').textContent=id;$('detail-body').replaceChildren();
    if(!$('detail').open)$('detail').showModal();
    location.hash = new URLSearchParams({card:id}).toString();
    try {const card=await api('/api/card/'+encodeURIComponent(id));if(sequence===detailRequest && selected===id)renderDetail(card);}
    catch(error){if(sequence===detailRequest)$('detail-body').append(section('Could not open card',error.message));}
  }
  async function load() {
    const sequence=++loadRequest;$('refresh').disabled=true;
    try {
      const next = await api('/api/cards');if(sequence!==loadRequest)return;
      const changed = JSON.stringify([next.cards,next.events,next.unscheduled]) !== lastRendered;
      lastRendered = JSON.stringify([next.cards,next.events,next.unscheduled]);data=next;
      $('error').hidden=true;$('freshness').textContent='Updated '+formatTime(next.as_of)+' · live records';
      if(changed || !$('content').children.length)render();
      if(selected && $('detail').open && changed && !dirty && !saving) {
        const id=selected;
        try {
          const card=await api('/api/card/'+encodeURIComponent(id));
          if(sequence===loadRequest && selected===card.id && !dirty && !saving)renderDetail(card);
        } catch(error) {
          if(sequence===loadRequest && selected===id)$('detail-body').replaceChildren(section('Card changed or unavailable',error.message));
        }
      }
      else if(selected && changed && dirty && $('edit-status'))$('edit-status').textContent='Records changed. Your unsaved text is kept; saving will check for conflicts.';
    } catch(error) {
      $('error').textContent=error.message+(data?' Showing the last loaded records.':'');$('error').hidden=false;
      $('freshness').textContent=data?'Last loaded '+formatTime(data.as_of)+' · refresh unavailable':'Not connected';
    } finally {if(sequence===loadRequest)$('refresh').disabled=false;}
  }
  $('grid-mode').addEventListener('click',()=>setMode('grid'));
  $('timeline-mode').addEventListener('click',()=>setMode('timeline'));
  function setMode(value){mode=value;$('grid-mode').setAttribute('aria-pressed',String(value==='grid'));$('timeline-mode').setAttribute('aria-pressed',String(value==='timeline'));render();}
  $('search').addEventListener('input',render);$('filter').addEventListener('change',render);$('refresh').addEventListener('click',load);
  function canClose(){return !saving && (!dirty || confirm('Discard your unsaved changes?'));}
  $('close-detail').addEventListener('click',()=>{if(canClose())$('detail').close();});
  $('detail').addEventListener('cancel',event=>{if(!canClose())event.preventDefault();});
  $('detail').addEventListener('close',()=>{selected=null;dirty=false;++detailRequest;history.replaceState(null,'',location.pathname);});
  $('detail').addEventListener('click',event=>{if(event.target===$('detail')){const rect=$('detail').getBoundingClientRect();if((event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)&&canClose())$('detail').close();}});
  window.addEventListener('beforeunload',event=>{if(dirty||saving){event.preventDefault();event.returnValue='';}});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)load();});
  setInterval(()=>{if(!document.hidden)load();},20000);
  load().then(()=>{if(params.has('card'))openDetail(params.get('card'));});
})();
