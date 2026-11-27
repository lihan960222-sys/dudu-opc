import test from "node:test";
import assert from "node:assert/strict";
import { clearConversationCaches } from "./history-reset.ts";

test("reset clears old Dudu threads but keeps new drafts, hosted workspaces and account settings", () => {
  const keep = ["suparagent:composer:v2:new", "suparagent:draft:new:agent:4",
    "suparagent:composer:v2:u_abc:12", "suparagent:theme", "suparagent:workspace"];
  const keys = [...keep, "suparagent:draft:3", "suparagent:composer:v2:7", "suparagent:composer:v2:sending:9",
    "suparagent:queued-follow-ups:11", "suparagent:reactions:12", "suparagent:thread-seen"];
  clearConversationCaches({ get length() { return keys.length; }, key: i => keys[i],
    removeItem: key => keys.splice(keys.indexOf(key), 1) });
  assert.deepEqual(keys, keep);
});
