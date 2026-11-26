// Bundle the add-on: strip the TypeScript types from each source file and concatenate them
// into one classic background script. Run: node build.mjs <output directory>
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../../../../..");
const sources = ["util.ts", "reads.ts", "writes.ts", "calendar.ts", "background.ts"];

function generateProtocol(...args) {
  const venvPython = path.join(root, ".venv", ...(process.platform === "win32" ? ["Scripts", "python.exe"] : ["bin", "python"]));
  const python = process.env.CHAMBERLAIN_PYTHON ?? (existsSync(venvPython) ? venvPython : "python");
  return execFileSync(python, ["-m", "chamberlain.connectors.thunderbird.typegen", ...args], { encoding: "utf8" });
}

export function bundle(protocolSource = generateProtocol("--stdout")) {
  const source = [protocolSource, ...sources.map((name) => readFileSync(path.join(here, "src", name), "utf8"))];
  return source.map((text) => stripTypeScriptTypes(text, { mode: "strip" })).join("\n");
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [out, protocolInput] = process.argv.slice(2);
  if (!out || (protocolInput && protocolInput !== "--protocol-stdin")) {
    console.error("usage: node build.mjs --types-only | <output directory> [--protocol-stdin]");
    process.exit(2);
  }
  if (out === "--types-only") {
    process.stdout.write(generateProtocol());
  } else {
    mkdirSync(out, { recursive: true });
    const protocolSource = protocolInput ? readFileSync(0, "utf8") : undefined;
    writeFileSync(path.join(out, "background.js"), bundle(protocolSource));
  }
}
