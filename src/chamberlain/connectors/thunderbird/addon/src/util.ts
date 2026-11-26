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


const HELPER_CODES: readonly BridgeErrorCode[] = ["bad_request", "account_mismatch", "not_found", "ambiguous", "changed", "busy", "timeout", "client_offline", "unsupported"];

/** A privileged helper's failure arrives as "chamberlain:<code>:<reason>" (see experiment/api.js). */
function helperFailure(text: string): { code: BridgeErrorCode; message: string } | null {
  const m = /^chamberlain:([a-z_]+):(.*)$/s.exec(text);
  if (!m || !HELPER_CODES.includes(m[1] as BridgeErrorCode)) return null;
  return { code: m[1] as BridgeErrorCode, message: m[2] || `The Thunderbird helper refused (${m[1]}).` };
}

function fail(code: BridgeErrorCode, message: string, mutation: "none" | "possible" = "none"): never {
  throw new BridgeFailure(code, message, mutation);
}

function check(condition: unknown, code: BridgeErrorCode, message: string, mutation: "none" | "possible" = "none"): asserts condition {
  if (!condition) fail(code, message, mutation);
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
