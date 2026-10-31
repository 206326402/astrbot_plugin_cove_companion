import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const sdk = require("NeteaseCloudMusicApi") as Record<string, unknown>;

const ACCOUNT_OPERATIONS = [
  "search",
  "user_playlist",
  "playlist_track_all",
  "playlist_create",
  "playlist_tracks",
  "like",
  "user_record",
  "recommend_songs",
] as const;

test("installed NeteaseCloudMusicApi exposes Cove account operations", () => {
  for (const operation of ACCOUNT_OPERATIONS) {
    assert.equal(typeof sdk[operation], "function", `${operation} should be available`);
  }
});
