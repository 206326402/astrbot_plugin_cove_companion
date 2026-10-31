import assert from "node:assert/strict";
import test from "node:test";
import { normalizeReplyBubbles } from "../src/replyBubbles.js";

test("keeps genuinely short single replies as one bubble", () => {
  assert.deepEqual(normalizeReplyBubbles(["嗯嗯 ^ ^"]), ["嗯嗯 ^ ^"]);
});

test("preserves model-provided multiple bubbles", () => {
  assert.deepEqual(
    normalizeReplyBubbles(["在呀 ^ ^", "刚刚还在修 Bridge", "你一叫我就过来了"]),
    ["在呀 ^ ^", "刚刚还在修 Bridge", "你一叫我就过来了"],
  );
});

test("splits an obviously long single reply into natural bubbles", () => {
  const input =
    "现在这条回复已经明显不适合挤在一个气泡里了。第一部分先说明发生了什么。第二部分再说我们准备怎么处理。最后留一句轻一点的收尾，让聊天室看起来更像自然聊天，而不是一整块说明文。";
  const bubbles = normalizeReplyBubbles([input]);
  assert.ok(bubbles.length >= 2);
  assert.ok(bubbles.length <= 5);
  assert.equal(bubbles.join(""), input);
});
