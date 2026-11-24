// Bundle the add-on: strip the TypeScript types from each source file and concatenate them
// into one classic background script. Run: node build.mjs <output directory>
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
export const SOURCES = ["protocol.generated.ts", "util.ts", "reads.ts", "writes.ts", "background.ts"];

export function bundle() {
  return SOURCES.map((name) => stripTypeScriptTypes(readFileSync(path.join(here, "src", name), "utf8"), { mode: "strip" })).join("\n");
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const out = process.argv[2];
  if (!out) {
    console.error("usage: node build.mjs <output directory>");
    process.exit(2);
  }
  mkdirSync(out, { recursive: true });
  writeFileSync(path.join(out, "background.js"), bundle());
}
