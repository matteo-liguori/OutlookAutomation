// Run inside functions.exec with `tools` and a google-prepare result as `request`.
// The connected read API is supplied by Codex; no browser or new credentials.
return (async () => {
  if (typeof request.timezone !== 'string' || !request.timezone) throw new Error('Use the timezone from calendar prepare');
  new Intl.DateTimeFormat('en', {timeZone: request.timezone});
  const allowed = new Set(request.allowed_sources || ["calendar:primary", "calendar:shared"]);
  if (!Array.isArray(request.sources) || request.sources.length > 2 || new Set(request.sources.map(s => s.source)).size !== request.sources.length || request.sources.some(s => !allowed.has(s.source))) throw new Error("Use unique configured Google sources");
  const started_at = new Date().toISOString();
  const sources = await Promise.all(request.sources.map(async source => {
    const pages = []; const seen = new Set(); let token = null; let error = null; let error_code = null;
    for (let i = 0; i < 10; i++) {
      const args = {calendar_id:source.calendar_id,time_min:request.start,time_max:request.end,timezone_str:request.timezone,max_results:100};
      if (token) args.next_page_token = token;
      try {
        const result = await tools.mcp__codex_apps__google_calendar_search_events(args);
        if (result.isError) {
          const detail=(result.content || []).filter(c=>c.type==="text").map(c=>c.text).join(" ");
          error_code=/401|unauthori[sz]ed|invalid_grant|expired|authenticate/i.test(detail)?"authentication-unavailable":/403|forbidden|permission/i.test(detail)?"permission-denied":"connector-error";
          throw new Error("Google calendar " + error_code);
        }
        const data = result.structuredContent;
        if (!data || !Array.isArray(data.events) || data.events.length>100) throw new Error("Google response has no verified bounded events array");
        const next = data.next_page_token || null;
        pages.push({requested_page_token:token,next_page_token:next,events:data.events,fetched_at:new Date().toISOString()});
        if (!next) return {...source,pages,complete:true,error:null,error_code:null};
        if (seen.has(next)) throw new Error("Repeated Google paging token");
        seen.add(next);token = next;
      } catch (e) { error = String(e.message || e).slice(0,600);break; }
    }
    return {...source,pages,complete:false,error:error || "Google page cap reached",error_code:error_code || "collection-incomplete"};
  }));
  return {schema:1,started_at,completed_at:new Date().toISOString(),start:request.start,end:request.end,timezone:request.timezone,sources};
})();
