// Run in functions.exec with `tools`, a fresh schedule-sync result as `request`,
// and `preflight` obtained by schedule sync --input - with that exact request.
// No file edits, private IPC, alternative scheduler or new model wake is used.
return (async () => {
  if (!preflight || preflight.verified !== true) throw new Error('Fresh harness preflight required');
  if (request.issues?.length || !Array.isArray(request.operations)) throw new Error('Resolve schedule sync issues first');
  const results = [];
  for (const operation of request.operations) {
    if (operation.arguments.id !== operation.id || !['update', 'delete'].includes(operation.arguments.mode)) throw new Error('Only bound update/delete operations are supported');
    const result = await tools.mcp__codex_app__automation_update(operation.arguments);
    if (result.isError || (result.content || []).some(c => c.type === 'text' && /invalid arguments|failed|not found/i.test(c.text))) throw new Error('App rejected a schedule update; stop and replan');
    results.push({id: operation.id, result});
  }
  return {applied: results, instruction: 'Run schedule sync again now; only its actual app readback can establish synced status.'};
})();
