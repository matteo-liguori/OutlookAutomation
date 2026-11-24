// Shared helpers. Classic script: every top-level name is global to the add-on.
// No eval, no Function constructor, no string timers (bridge contract B-3).

class BridgeFailure extends Error {
  code: BridgeErrorCode;
  mutation: "none" | "possible";
  constructor(code: BridgeErrorCode, message: string, mutation: "none" | "possible" = "none") {
    super(message);
    this.code = code;
    this.mutation = mutation;
  }
}


function fail(code: BridgeErrorCode, message: string, mutation: "none" | "possible" = "none"): never {
  throw new BridgeFailure(code, message, mutation);
}

function check(condition: unknown, code: BridgeErrorCode, message: string): asserts condition {
  if (!condition) fail(code, message);
}

function clip(value: unknown, limit: number): string {
  return String(value ?? "").slice(0, limit);
}

/** Message-IDs cross the bridge bare, without angle brackets. */
function bare(id: string): string {
  return String(id).replace(/^<|>$/g, "");
}

/** Reply prefixes and case do not count in a subject comparison. */
function normaliseSubject(value: unknown): string {
  return String(value ?? "")
    .replace(/^(?:(?:re|fw|fwd)\s*:\s*)+/gi, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

/** CRLF to LF, ASCII whitespace trimmed; the same function as `normalise_body` in the connector. */
function normaliseBody(value: unknown): string {
  return String(value ?? "").replace(/\r\n?/g, "\n").replace(/^[ \t\n]+|[ \t\n]+$/g, "");
}

async function bodyDigest(value: unknown): Promise<string> {
  return sha256Hex(new TextEncoder().encode(normaliseBody(value)));
}

async function sha256Hex(bytes: ArrayBuffer | Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes as BufferSource);
  return [...new Uint8Array(digest)].map((v) => v.toString(16).padStart(2, "0")).join("");
}

function bytesToBase64(bytes: Uint8Array): string {
  let text = "";
  for (let i = 0; i < bytes.length; i += 8192) {
    text += String.fromCharCode(...bytes.subarray(i, i + 8192));
  }
  return btoa(text);
}

function base64ToBytes(text: string): Uint8Array {
  const raw = atob(text);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function sortedJson(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(sortedJson).join(",") + "]";
  if (value !== null && typeof value === "object") {
    const record = value as { [key: string]: unknown };
    return (
      "{" +
      Object.keys(record)
        .sort()
        .map((key) => JSON.stringify(key) + ":" + sortedJson(record[key]))
        .join(",") +
      "}"
    );
  }
  return JSON.stringify(value);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// --- request validation against the generated JSON Schema (protocol.py is the source) ---

function resolveSchema(node: any): any {
  if (node && typeof node.$ref === "string") {
    const name = node.$ref.slice(node.$ref.lastIndexOf("/") + 1);
    const target = SCHEMA_DEFS[name];
    check(target, "internal", "schema reference missing: " + name);
    return target;
  }
  return node;
}

/** Return a list of problems; empty means the value satisfies the schema subset we generate. */
function validateSchema(value: any, schemaNode: any, path: string): string[] {
  const node = resolveSchema(schemaNode);
  if (node === true || (node && Object.keys(node).length === 0)) return [];
  if ("const" in node) return value === node.const ? [] : [path + ": must be " + JSON.stringify(node.const)];
  if (node.enum) return node.enum.includes(value) ? [] : [path + ": not an allowed value"];
  const options = node.anyOf || node.oneOf;
  if (options) {
    const attempts = options.map((option: any) => validateSchema(value, option, path));
    return attempts.some((a: string[]) => a.length === 0) ? [] : [path + ": matches no allowed shape"];
  }
  const problems: string[] = [];
  switch (node.type) {
    case "string":
      if (typeof value !== "string") return [path + ": expected a string"];
      if (node.minLength !== undefined && value.length < node.minLength) problems.push(path + ": too short");
      if (node.maxLength !== undefined && value.length > node.maxLength) problems.push(path + ": too long");
      if (node.pattern && !new RegExp(node.pattern).test(value)) problems.push(path + ": wrong format");
      return problems;
    case "integer":
    case "number":
      if (typeof value !== "number" || !Number.isFinite(value)) return [path + ": expected a number"];
      if (node.type === "integer" && !Number.isInteger(value)) problems.push(path + ": expected an integer");
      if (node.minimum !== undefined && value < node.minimum) problems.push(path + ": too small");
      if (node.maximum !== undefined && value > node.maximum) problems.push(path + ": too large");
      return problems;
    case "boolean":
      return typeof value === "boolean" ? [] : [path + ": expected a boolean"];
    case "null":
      return value === null ? [] : [path + ": expected null"];
    case "array": {
      if (!Array.isArray(value)) return [path + ": expected a list"];
      if (node.minItems !== undefined && value.length < node.minItems) problems.push(path + ": too few items");
      if (node.maxItems !== undefined && value.length > node.maxItems) problems.push(path + ": too many items");
      value.forEach((item: any, index: number) => problems.push(...validateSchema(item, node.items ?? {}, path + "[" + index + "]")));
      return problems;
    }
    case "object": {
      if (value === null || typeof value !== "object" || Array.isArray(value)) return [path + ": expected an object"];
      const properties = node.properties;
      if (!properties) {
        for (const key of Object.keys(value)) problems.push(...validateSchema(value[key], node.additionalProperties ?? {}, path + "." + key));
        return problems;
      }
      for (const name of node.required || []) if (!(name in value)) problems.push(path + "." + name + ": required");
      for (const key of Object.keys(value)) {
        if (!(key in properties)) {
          if (node.additionalProperties === false) problems.push(path + "." + key + ": not allowed");
        } else problems.push(...validateSchema(value[key], properties[key], path + "." + key));
      }
      return problems;
    }
    default:
      return [path + ": unsupported schema"];
  }
}

/** Validate and return the args with defaults applied. */
function validateArgs(cmd: string, args: unknown): any {
  const name = ARGS_SCHEMA_NAME[cmd];
  check(name, "unknown_command", "no such command");
  const schema = { $ref: "#/$defs/" + name };
  const filled = applyDefaults(args, schema);
  const problems = validateSchema(filled, schema, "args");
  if (problems.length) fail("bad_request", problems.slice(0, 5).join("; "));
  return filled;
}

/** Fill schema defaults so handlers see every defaulted field the generated types promise. */
function applyDefaults(value: any, schemaNode: any): any {
  const node = resolveSchema(schemaNode);
  const options = node?.anyOf || node?.oneOf;
  if (options && value !== null && typeof value === "object") {
    for (const option of options) {
      const resolved = resolveSchema(option);
      if (resolved?.type === "object" || resolved?.type === "array") return applyDefaults(value, resolved);
    }
    return value;
  }
  if (Array.isArray(value) && node?.items) return value.map((item) => applyDefaults(item, node.items));
  if (value !== null && typeof value === "object" && node?.properties) {
    const out: { [key: string]: any } = { ...value };
    for (const [key, child] of Object.entries<any>(node.properties)) {
      if (!(key in out) && "default" in child && child.default !== null) out[key] = JSON.parse(JSON.stringify(child.default));
      else if (key in out) out[key] = applyDefaults(out[key], child);
    }
    return out;
  }
  return value;
}
