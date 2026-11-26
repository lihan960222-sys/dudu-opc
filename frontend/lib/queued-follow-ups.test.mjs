import assert from "node:assert/strict";
import test from "node:test";
import {
  loadQueuedFollowUps,
  newQueuedFollowUp,
  saveQueuedFollowUps,
  subscribeQueuedFollowUps,
} from "./queued-follow-ups.ts";

class MemoryStorage {
  values = new Map();
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

globalThis.localStorage = new MemoryStorage();

test("a quoted reply keeps its original turn across a page reload", () => {
  const quoted = newQueuedFollowUp("继续", 14);
  localStorage.setItem("suparagent:queued-follow-ups:982", JSON.stringify([quoted]));
  assert.equal(loadQueuedFollowUps(982)[0].replyToTurnId, 14);
});

test("queued follow-ups expose stable snapshots and notify subscribers", () => {
  const threadId = 901;
  let updates = 0;
  const unsubscribe = subscribeQueuedFollowUps(threadId, () => { updates += 1; });

  assert.deepEqual(loadQueuedFollowUps(threadId), []);
  const items = [{ id: "q1", text: "Check the timeout behavior" }];
  saveQueuedFollowUps(threadId, items);

  const snapshot = loadQueuedFollowUps(threadId);
  assert.deepEqual(snapshot, items);
  assert.strictEqual(loadQueuedFollowUps(threadId), snapshot);
  assert.equal(updates, 1);

  unsubscribe();
  saveQueuedFollowUps(threadId, []);
  assert.equal(updates, 1);
  assert.deepEqual(loadQueuedFollowUps(threadId), []);
});
