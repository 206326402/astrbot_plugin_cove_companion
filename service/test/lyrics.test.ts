import assert from "node:assert/strict";
import test from "node:test";
import { findLyricWindow, parseLrc } from "../src/netease/lyrics.js";

test("parses ordinary LRC timestamps", () => {
  const timeline = parseLrc("[00:01.00]第一句\n[01:02.50]第二句");
  assert.deepEqual(timeline.lines, [
    { timeMs: 1000, text: "第一句" },
    { timeMs: 62_500, text: "第二句" },
  ]);
});

test("parses millisecond LRC timestamps", () => {
  const timeline = parseLrc("[00:01.007]七毫秒\n[00:02.345]三百四十五毫秒");
  assert.deepEqual(timeline.lines, [
    { timeMs: 1007, text: "七毫秒" },
    { timeMs: 2345, text: "三百四十五毫秒" },
  ]);
});

test("ignores empty and metadata lines while preserving raw lyrics", () => {
  const raw = "[ar:歌手]\n[ti:歌曲]\n[00:01.00]\n\n[00:02.00]有效歌词";
  const timeline = parseLrc(raw);
  assert.equal(timeline.raw, raw);
  assert.deepEqual(timeline.lines, [{ timeMs: 2000, text: "有效歌词" }]);
});

test("selects previous current and next lyrics between timestamps", () => {
  const lines = parseLrc("[00:01]一\n[00:02]二\n[00:03]三").lines;
  assert.deepEqual(findLyricWindow(lines, 2500), {
    previous: "一",
    current: "二",
    next: "三",
  });
});

test("returns the first line as next before lyrics begin", () => {
  const lines = parseLrc("[00:01]一\n[00:02]二").lines;
  assert.deepEqual(findLyricWindow(lines, 500), { next: "一" });
});

test("keeps the final line current after lyrics end", () => {
  const lines = parseLrc("[00:01]一\n[00:02]二\n[00:03]三").lines;
  assert.deepEqual(findLyricWindow(lines, 10_000), {
    previous: "二",
    current: "三",
  });
});
