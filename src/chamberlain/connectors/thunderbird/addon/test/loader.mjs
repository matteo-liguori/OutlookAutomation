// Run the real add-on source (type-stripped TypeScript) against a fake Thunderbird.
import { File } from "node:buffer";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { bundle } from "../build.mjs";

const generated = process.env.CHAMBERLAIN_ADDON_TYPES;
const addonSource = bundle(generated ? readFileSync(generated, "utf8") : undefined);

/** Boot the add-on. `sink` receives every frame the add-on posts to the native host. */
export function startAddon(world, { sink, readMs = 30000, writeMs = 45000, assemblyMs = 60000, tuning = {} } = {}) {
  const posted = [];
  const listeners = [];
  const port = {
    postMessage: (frame) => {
      const copy = JSON.parse(JSON.stringify(frame));
      posted.push(copy);
      if (sink) sink(copy);
    },
    onMessage: { addListener: (l) => listeners.push(l) },
    onDisconnect: { addListener: () => {} },
  };
  const scale = new Map([
    [30000, readMs],
    [45000, writeMs],
    [60000, assemblyMs],
  ]);
  const messenger = { ...world.messenger, runtime: { ...world.messenger.runtime, connectNative: () => port } };
  const context = vm.createContext({
    messenger,
    console,
    structuredClone,
    crypto: globalThis.crypto,
    File,
    TextEncoder,
    btoa,
    atob,
    Date,
    setTimeout: (f, ms) => setTimeout(f, scale.get(ms) ?? ms),
    clearTimeout,
  });
  new vm.Script(addonSource, { filename: "addon.js" }).runInContext(context);
  vm.runInContext(`Object.assign(TUNING, ${JSON.stringify({ movePollMs: 5, savePollMs: 5, ...tuning })})`, context);
  return {
    context,
    posted,
    protocolVersion: vm.runInContext("PROTOCOL_VERSION", context),
    deliver: (frame) => listeners.forEach((l) => l(frame)),
    setBusy: (value) => vm.runInContext(`writeBusy = ${value}`, context),
    eval: (code) => vm.runInContext(code, context),
  };
}

let counter = 0;
export function requestId() {
  counter += 1;
  return "req" + String(counter).padStart(8, "0");
}

export async function untilResponse(addon, id, ms = 5000) {
  const started = Date.now();
  for (;;) {
    const found = addon.posted.find((f) => f.t === "response" && f.id === id);
    if (found) return found;
    if (Date.now() - started > ms) throw new Error("no response for " + id);
    await new Promise((r) => setTimeout(r, 2));
  }
}

export const BASE = { account_id: "account1", mailbox: "owner@example.org" };

/** Send a request and wait for its response frame. */
export async function call(addon, cmd, args, { id = requestId(), chunks = [] } = {}) {
  const v = addon.protocolVersion;
  addon.deliver({ t: "request", v, id, cmd, args });
  for (const chunk of chunks) addon.deliver({ t: "chunk", v, id, ...chunk });
  return untilResponse(addon, id);
}
