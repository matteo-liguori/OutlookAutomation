const fs=require('fs'),assert=require('assert');
const collect=new Function('tools','request',fs.readFileSync('monitor/integrations/collect_google.js','utf8'));
const request={start:'2026-10-13T00:00:00+01:00',end:'2026-10-17T00:00:00+01:00',sources:[{source:'calendar:primary',calendar_id:'primary'},{source:'calendar:shared',calendar_id:'flat-510'}]};
(async()=>{
  let calls=[];
  let out=await collect({mcp__codex_apps__google_calendar_search_events:async args=>{
    calls.push(args);assert.equal(args.max_results,100);assert.equal(args.timezone_str,'Europe/London');assert.equal(args.time_min,request.start);
    return {structuredContent:{events:[],next_page_token:args.calendar_id==='primary'&&!args.next_page_token?'next':null}};
  }},request);
  assert(out.sources.every(s=>s.complete));assert.equal(calls.length,3);assert.equal(out.sources[0].pages[1].requested_page_token,'next');
  out=await collect({mcp__codex_apps__google_calendar_search_events:async args=>args.calendar_id==='primary'?{isError:true,content:[{type:'text',text:'401 invalid_grant'}]}:{structuredContent:{events:[]}}},request);
  assert.equal(out.sources[0].error_code,'authentication-unavailable');assert(out.sources[1].complete);
  out=await collect({mcp__codex_apps__google_calendar_search_events:async()=>({structuredContent:{events:[],next_page_token:'repeated'}})},request);
  assert(out.sources.every(s=>!s.complete&&s.pages.length===2));
  let n=0;out=await collect({mcp__codex_apps__google_calendar_search_events:async()=>({structuredContent:{events:[],next_page_token:String(++n)}})},request);
  assert(out.sources.every(s=>!s.complete&&s.pages.length===10));
  out=await collect({mcp__codex_apps__google_calendar_search_events:async()=>({structuredContent:{wrong:[]}})},request);assert(out.sources.every(s=>!s.complete));
  await assert.rejects(()=>collect({}, {...request,sources:[{source:'calendar:IM'},{source:'calendar:primary'}]}));
  process.stdout.write(JSON.stringify({assertions:18,scenarios:6}));
})().catch(e=>{process.stderr.write(String(e.stack));process.exitCode=1});
