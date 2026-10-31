import {
  asRecord,
  readNumber,
  readString,
  type JsonRecord,
  type TogetherInvite,
} from "./types.js";

function decodeRepeatedly(value: string): string {
  let current = value;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const decoded = decodeURIComponent(current);
      if (decoded === current) break;
      current = decoded;
    } catch {
      break;
    }
  }
  return current;
}

function findNativeUrl(value: unknown, depth = 0): string | null {
  if (depth > 6 || value === null || typeof value !== "object") return null;
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findNativeUrl(item, depth + 1);
      if (found) return found;
    }
    return null;
  }

  for (const [key, child] of Object.entries(value as JsonRecord)) {
    if (key.toLowerCase() === "nativeurl") {
      const found = readString(child);
      if (found) return found;
    }
    const nested = findNativeUrl(child, depth + 1);
    if (nested) return nested;
  }
  return null;
}

function findMessages(value: unknown, depth = 0): unknown[] {
  if (depth > 5 || value === null || typeof value !== "object") return [];
  if (Array.isArray(value)) {
    if (value.some((item) => {
      const record = asRecord(item);
      return "msg" in record && ("time" in record || "type" in record);
    })) return value;
    for (const item of value) {
      const found = findMessages(item, depth + 1);
      if (found.length) return found;
    }
    return [];
  }

  const record = value as JsonRecord;
  for (const key of ["msgs", "messages", "data"]) {
    if (key in record) {
      const found = findMessages(record[key], depth + 1);
      if (found.length) return found;
    }
  }
  return [];
}

export function parseLatestInvite(
  history: JsonRecord,
  contactUid: string,
  now = Date.now(),
): TogetherInvite | null {
  const messages = findMessages(history)
    .map(asRecord)
    .sort((a, b) => (readNumber(b.time) ?? 0) - (readNumber(a.time) ?? 0));

  for (const message of messages) {
    const type = readNumber(message.type);
    if (type !== null && type !== 23) continue;
    let messageTime = readNumber(message.time) ?? 0;
    if (messageTime > 0 && messageTime < 10_000_000_000) messageTime *= 1000;
    if (!messageTime || now - messageTime > 5 * 60_000) continue;

    const rawPayload = readString(message.msg);
    if (!rawPayload) continue;
    let payload: unknown;
    try {
      payload = JSON.parse(rawPayload);
    } catch {
      continue;
    }

    const nativeUrl = findNativeUrl(payload);
    if (!nativeUrl || !/listenTogether/i.test(nativeUrl)) continue;
    const decoded = decodeRepeatedly(nativeUrl);
    const roomId = decoded.match(/[?&]roomId=([^&#]+)/i)?.[1];
    const inviterId = decoded.match(/[?&]inviterId=([^&#]+)/i)?.[1] ?? contactUid;
    if (!roomId || !inviterId) continue;
    return {
      roomId: decodeRepeatedly(roomId),
      inviterId: decodeRepeatedly(inviterId),
      messageTime,
    };
  }
  return null;
}

export function collectContactUserIds(value: unknown, ownUserId: string): string[] {
  const result = new Set<string>();
  const idKeys = new Set(["userid", "uid", "fromuserid", "touserid"]);

  function visit(current: unknown, depth: number) {
    if (depth > 7 || current === null || typeof current !== "object") return;
    if (Array.isArray(current)) {
      current.forEach((item) => visit(item, depth + 1));
      return;
    }
    for (const [key, child] of Object.entries(current as JsonRecord)) {
      if (idKeys.has(key.toLowerCase())) {
        const id = readString(child);
        if (id && id !== ownUserId) result.add(id);
      }
      if (typeof child === "object" && child !== null) visit(child, depth + 1);
    }
  }

  visit(value, 0);
  return [...result].slice(0, 10);
}
