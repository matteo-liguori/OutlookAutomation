// Command bridge (protocol v2). Credentials stay in Thunderbird. No journal, no retry,
// no command outside the generated closed list (bridge contract B-2 to B-5).

const ADDON_VERSION = "2.0.0";
const HOST_NAME = "chamberlain_thunderbird_bridge";
const READ_DEADLINE_MS = 30_000;
const WRITE_DEADLINE_MS = 45_000;
const ASSEMBLY_DEADLINE_MS = 60_000;
const RECONNECT_MS = 1_500;

let port: TbPort | undefined;
let writeBusy = false;
const inFlight = new Set<string>();

interface Assembly {
  request: RequestFrame;
  specs: AttachmentSpec[];
  received: Map<string, { next: number; parts: Uint8Array[] }>;
  timer: ReturnType<typeof setTimeout>;
}
const assemblies = new Map<string, Assembly>();

function advertisedCapabilities(): string[] {
  const m: any = messenger;
  const have: { [name: string]: boolean } = {
    read: !!m.messages?.getFull,
    search: !!m.messages?.query,
    attachments: !!m.messages?.getAttachmentFile,
    send: !!m.compose?.sendMessage && !!m.harnessMail,
    draft: !!m.compose?.saveMessage && !!m.harnessMail,
    move: !!m.messages?.move,
    trash: !!m.messages?.move,
    mark_read: !!m.messages?.update,
    contacts: !!m.addressBooks,
    refresh: !!m.harnessMail?.syncFolders,
    calendar: calendarSupported(),
  };
  return ADVERTISED_CAPABILITIES.filter((name) => have[name]);
}

function post(frame: unknown): void {
  port?.postMessage(frame);
}

function respondOk(id: string, cmd: string, result: unknown, chunks = 0): void {
  post({ t: "response", v: PROTOCOL_VERSION, id, cmd, ok: true, result, chunks });
}

function respondError(id: string, cmd: string, error: BridgeError): void {
  post({ t: "response", v: PROTOCOL_VERSION, id, cmd, ok: false, error, chunks: 0 });
}

function toBridgeError(error: unknown, ctx: Ctx | null): BridgeError {
  const issued = ctx?.mutationIssued === true;
  if (error instanceof BridgeFailure) {
    return { code: error.code, message: clip(error.message, 500), mutation: issued || error.mutation === "possible" ? "possible" : "none", retryable: error.code === "busy" };
  }
  const text = clip((error as Error)?.message ?? error, 500);
  return { code: /offline/i.test(text) ? "client_offline" : "internal", message: text, mutation: issued ? "possible" : "none", retryable: false };
}

async function runCommand(frame: RequestFrame, ctx: Ctx): Promise<{ result: unknown; chunks: ChunkFrame[] }> {
  const args: any = frame.args;
  const plain = async (value: Promise<unknown>) => ({ result: await value, chunks: [] as ChunkFrame[] });
  switch (frame.cmd) {
    case "status":
      return plain(cmdStatus());
    case "accounts":
      return plain(cmdAccounts());
    case "folders":
      return plain(cmdFolders(args));
    case "query":
      return plain(cmdQuery(args));
    case "read":
      return plain(cmdRead(args));
    case "attachment":
      return cmdAttachment(args, frame.id);
    case "resolve":
      return plain(cmdResolve(args));
    case "refresh":
      return plain(cmdRefresh(args));
    case "contacts":
      return plain(cmdContacts(args));
    case "send":
      return plain(cmdCompose(args, ctx, "send"));
    case "draft_save":
      return plain(cmdCompose(args, ctx, "draft_save"));
    case "draft_update":
      return plain(cmdCompose(args, ctx, "draft_update"));
    case "draft_discard":
      return plain(cmdDraftDiscard(args));
    case "move":
      return plain(cmdMove(args, ctx, false));
    case "trash":
      return plain(cmdMove(args, ctx, true));
    case "mark_read":
      return plain(cmdMarkRead(args, ctx));
    case "reconcile_send":
      return plain(cmdReconcileCompose(args, "send"));
    case "reconcile_draft_save":
      return plain(cmdReconcileCompose(args, "draft_save"));
    case "reconcile_draft_update":
      return plain(cmdReconcileCompose(args, "draft_update"));
    case "reconcile_draft_discard":
      return plain(cmdReconcileDiscard(args));
    case "reconcile_move":
    case "reconcile_trash":
      return plain(cmdReconcileMove(args));
    case "reconcile_mark_read":
      return plain(cmdReconcileMarkRead(args));
    case "calendar_list":
      return plain(cmdCalendarList());
    case "calendar_query":
      return plain(cmdCalendarQuery(args));
    case "calendar_read":
      return plain(cmdCalendarRead(args));
    case "calendar_create":
      return plain(cmdCalendarWrite(args, ctx, "create"));
    case "calendar_update":
      return plain(cmdCalendarWrite(args, ctx, "update"));
    case "calendar_delete":
      return plain(cmdCalendarWrite(args, ctx, "delete"));
    default:
      return fail("unknown_command", "No such command.");
  }
}

function execute(frame: RequestFrame, files: Map<string, File>): void {
  const isWrite = WRITE_COMMANDS.includes(frame.cmd);
  if (isWrite && writeBusy) {
    respondError(frame.id, frame.cmd, { code: "busy", message: "Another write is in progress.", mutation: "none", retryable: true });
    inFlight.delete(frame.id);
    return;
  }
  if (isWrite) writeBusy = true;
  const ctx: Ctx = { id: frame.id, cancelled: false, mutationIssued: false, files };
  // Keep write-busy until the task really settles, even after its deadline passed:
  // a timed-out operation may still finish inside the client.
  const task = runCommand(frame, ctx).finally(() => {
    if (isWrite) writeBusy = false;
  });
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      ctx.cancelled = true;
      reject(new BridgeFailure("timeout", "The Thunderbird operation did not finish in time."));
    }, isWrite ? WRITE_DEADLINE_MS : READ_DEADLINE_MS);
  });
  Promise.race([task, deadline])
    .then(
      ({ result, chunks }) => {
        for (const chunk of chunks) post(chunk);
        respondOk(frame.id, frame.cmd, result, chunks.length);
      },
      (error) => {
        respondError(frame.id, frame.cmd, toBridgeError(error, ctx));
      },
    )
    .catch(() => undefined)
    .finally(() => {
      clearTimeout(timer);
      inFlight.delete(frame.id);
    });
  // A task that settles after its deadline must not leave an unhandled rejection behind.
  task.catch(() => undefined);
}

function onRequest(frame: RequestFrame): void {
  if (inFlight.has(frame.id)) {
    respondError(frame.id, frame.cmd, { code: "bad_request", message: "That request ID is already in flight.", mutation: "none", retryable: false });
    return;
  }
  if (!COMMANDS.includes(frame.cmd)) {
    respondError(frame.id, frame.cmd, { code: "unknown_command", message: "No such command.", mutation: "none", retryable: false });
    return;
  }
  let filled: RequestFrame;
  try {
    filled = { ...frame, args: validateArgs(frame.cmd, frame.args) };
  } catch (error) {
    respondError(frame.id, frame.cmd, toBridgeError(error, null));
    return;
  }
  frame = filled;
  inFlight.add(frame.id);
  const specs: AttachmentSpec[] = Array.isArray((frame.args as any).attachments) ? (frame.args as any).attachments : [];
  if (!specs.length) {
    execute(frame, new Map());
    return;
  }
  const keys = new Set(specs.map((s) => s.key));
  if (keys.size !== specs.length || !WRITE_COMMANDS.includes(frame.cmd)) {
    respondError(frame.id, frame.cmd, { code: "bad_request", message: "Attachment keys must be unique and only writes carry attachments.", mutation: "none", retryable: false });
    inFlight.delete(frame.id);
    return;
  }
  const timer = setTimeout(() => {
    assemblies.delete(frame.id);
    inFlight.delete(frame.id);
    respondError(frame.id, frame.cmd, { code: "timeout", message: "The attachment transfer did not finish.", mutation: "none", retryable: false });
  }, ASSEMBLY_DEADLINE_MS);
  assemblies.set(frame.id, { request: frame, specs, received: new Map(specs.map((s) => [s.key, { next: 0, parts: [] }])), timer });
  void finishAssembly(frame.id);
}

async function abortAssembly(id: string, message: string, code: BridgeErrorCode): Promise<void> {
  const assembly = assemblies.get(id);
  if (!assembly) return;
  clearTimeout(assembly.timer);
  assemblies.delete(id);
  inFlight.delete(id);
  respondError(id, assembly.request.cmd, { code, message, mutation: "none", retryable: false });
}

async function onChunk(frame: ChunkFrame): Promise<void> {
  const assembly = assemblies.get(frame.id);
  if (!assembly) return;
  const spec = assembly.specs.find((s) => s.key === frame.name);
  const state = assembly.received.get(frame.name);
  if (!spec || !state || frame.index !== state.next || frame.index >= spec.chunks) {
    await abortAssembly(frame.id, "An attachment chunk arrived out of order.", "bad_request");
    return;
  }
  let bytes: Uint8Array;
  try {
    bytes = base64ToBytes(frame.data);
  } catch {
    await abortAssembly(frame.id, "An attachment chunk was not valid base64.", "bad_request");
    return;
  }
  if (bytes.length > CHUNK_BYTES || (await sha256Hex(bytes)) !== frame.sha256) {
    await abortAssembly(frame.id, "An attachment chunk does not match its hash.", "hash_mismatch");
    return;
  }
  state.parts.push(bytes);
  state.next += 1;
  await finishAssembly(frame.id);
}

async function finishAssembly(id: string): Promise<void> {
  const assembly = assemblies.get(id);
  if (!assembly) return;
  for (const spec of assembly.specs) {
    if ((assembly.received.get(spec.key)?.next ?? 0) < spec.chunks) return;
  }
  clearTimeout(assembly.timer);
  assemblies.delete(id);
  const files = new Map<string, File>();
  for (const spec of assembly.specs) {
    const file = new File(assembly.received.get(spec.key)!.parts as BlobPart[], spec.name, { type: spec.content_type });
    if (file.size !== spec.size || (await sha256Hex(await file.arrayBuffer())) !== spec.sha256) {
      inFlight.delete(id);
      respondError(id, assembly.request.cmd, { code: "hash_mismatch", message: "An attachment does not match its announced hash.", mutation: "none", retryable: false });
      return;
    }
    files.set(spec.key, file);
  }
  execute(assembly.request, files);
}

let chunkQueue: Promise<void> = Promise.resolve();

function onFrame(value: unknown): void {
  const frame = value as { t?: string; v?: number; id?: string };
  if (!frame || typeof frame !== "object" || typeof frame.id !== "string") return;
  if (frame.v !== PROTOCOL_VERSION) {
    post({ t: "refusal", v: PROTOCOL_VERSION, id: frame.id, code: "version_mismatch", message: "Unsupported protocol version." });
    return;
  }
  if (frame.t === "request") onRequest(value as RequestFrame);
  else if (frame.t === "chunk") chunkQueue = chunkQueue.then(() => onChunk(value as ChunkFrame)).catch(() => undefined);
  // Anything else (welcome, refusal) is informational; the add-on never executes it.
}

function connect(): void {
  port = messenger.runtime.connectNative(HOST_NAME);
  port.onMessage.addListener(onFrame);
  port.onDisconnect.addListener(() => {
    port = undefined;
    inFlight.clear();
    for (const assembly of assemblies.values()) clearTimeout(assembly.timer);
    assemblies.clear();
    setTimeout(connect, RECONNECT_MS);
  });
  messenger.runtime.getBrowserInfo().then(
    (info) => post({ t: "addon_hello", v: PROTOCOL_VERSION, id: "hello" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8), addon_version: ADDON_VERSION, browser_version: info.version, capabilities: advertisedCapabilities() }),
    () => undefined,
  );
}

connect();
