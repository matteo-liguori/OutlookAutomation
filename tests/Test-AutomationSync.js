const fs=require('fs'),assert=require('assert');
const apply=new Function('tools','request','preflight',fs.readFileSync('monitor/integrations/sync_automations.js','utf8'));
(async()=>{
  const request={issues:[],operations:[{id:'owned',arguments:{mode:'update',id:'owned',rrule:'fixture',status:'PAUSED'}}]};
  const calls=[];
  const tools={mcp__codex_app__automation_update:async a=>{calls.push(a);return {content:[{type:'text',text:'Updated automation'}]};}};
  await assert.rejects(()=>apply(tools,request,{}));assert.equal(calls.length,0);
  const result=await apply(tools,request,{verified:true});assert.equal(result.applied.length,1);
  assert.deepEqual(calls[0],request.operations[0].arguments);
  await assert.rejects(()=>apply(tools,{...request,issues:[{error:'changed'}]},{verified:true}));
  await assert.rejects(()=>apply(tools,{issues:[],operations:[{id:'owned',arguments:{mode:'create',id:'other'}}]},{verified:true}));
  await assert.rejects(()=>apply({mcp__codex_app__automation_update:async()=>({isError:true})},request,{verified:true}));
  console.log('Shared app executor checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
