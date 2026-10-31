import assert from "node:assert/strict";
import test from "node:test";
import { NeteaseClient } from "../src/netease/client.js";

test("realtime credential request adds the NetEase macOS client context", async () => {
  const originalFetch = globalThis.fetch;
  const seen: { url?: string; headers?: Headers } = {};

  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    seen.url = String(input);
    seen.headers = new Headers(init?.headers);

    return new Response(JSON.stringify({
      code: 200,
      data: {
        accId: "nim-account",
        token: "nim-token",
        addr: ["nim.example.test"],
      },
    }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;

  try {
    const client = new NeteaseClient(
      "MUSIC_U=secret; __csrf=csrf; os=android; appver=old; requestId=old",
    );
    const credentials = await client.getRealtimeCredentials();

    assert.deepEqual(credentials, {
      accId: "nim-account",
      token: "nim-token",
      addresses: ["nim.example.test"],
    });

    assert.equal(
      seen.url,
      "https://interface3.music.163.com/api/middle/im/token/get?bizName=music_listenTogether",
    );

    const cookie = seen.headers?.get("cookie") ?? "";
    assert.match(cookie, /(?:^|; )MUSIC_U=secret(?:;|$)/);
    assert.match(cookie, /(?:^|; )__csrf=csrf(?:;|$)/);
    assert.match(cookie, /(?:^|; )os=osx(?:;|$)/);
    assert.match(cookie, /(?:^|; )osver=15\.5(?:;|$)/);
    assert.match(cookie, /(?:^|; )appver=3\.1\.10\.5100(?:;|$)/);
    assert.match(cookie, /(?:^|; )channel=netease(?:;|$)/);
    assert.match(cookie, /(?:^|; )versioncode=140(?:;|$)/);
    assert.match(cookie, /(?:^|; )buildver=\d+(?:;|$)/);
    assert.match(cookie, /(?:^|; )resolution=1920x1080(?:;|$)/);
    assert.match(cookie, /(?:^|; )requestId=\d+_\d{4}(?:;|$)/);

    assert.equal((cookie.match(/(?:^|; )os=/g) ?? []).length, 1);
    assert.equal((cookie.match(/(?:^|; )appver=/g) ?? []).length, 1);
    assert.equal((cookie.match(/(?:^|; )requestId=/g) ?? []).length, 1);

    assert.match(
      seen.headers?.get("user-agent") ?? "",
      /Macintosh; Intel Mac OS X 10_15_7/,
    );
    assert.equal(seen.headers?.get("accept-language"), "zh-CN,zh;q=0.9");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("realtime credential request keeps NetEase business errors visible", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    code: 301,
    message: "系统错误",
  }), {
    status: 200,
    headers: { "content-type": "application/json" },
  })) as typeof fetch;

  try {
    const client = new NeteaseClient("MUSIC_U=secret; __csrf=csrf");
    await assert.rejects(
      () => client.getRealtimeCredentials(),
      (error: unknown) => (
        error instanceof Error
        && error.name === "NeteaseApiError"
        && error.message === "系统错误"
      ),
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
