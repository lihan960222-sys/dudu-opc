import test from "node:test";
import assert from "node:assert/strict";
import { canManageProjectTerminal, directoryMode, directoryModes } from "./directory-mode.ts";

test("the project terminal control is visible only to the signed-in owner", () => {
  const owner = {kind: "browser", session_id: 2, role: "owner", person_id: 1, scopes: ["admin"]};
  assert.equal(canManageProjectTerminal(owner), true);
  for (const session of [null, {...owner, kind: "device"}, {...owner, session_id: null},
    {...owner, role: "member"}, {...owner, role: null, person_id: null},
    {...owner, person_id: null}, {...owner, scopes: ["chat"]}]) {
    assert.equal(canManageProjectTerminal(session), false);
  }
});

test("OPC directory modes preserve scope when migrating legacy settings", () => {
  assert.deepEqual(Object.keys(directoryModes), ["plan", "ask", "accept_edits"]);
  for (const key of Object.keys(directoryModes)) assert.equal(directoryMode(key), key);
  assert.equal(directoryMode("full_access"), "accept_edits");
  assert.equal(directoryMode("approve_for_me"), "ask");
  assert.equal(directoryMode("unknown"), "ask");
});
