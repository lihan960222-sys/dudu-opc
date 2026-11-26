import test from "node:test";
import assert from "node:assert/strict";
import { directoryMode, directoryModes } from "./directory-mode.ts";

test("OPC directory modes preserve scope when migrating legacy settings", () => {
  assert.deepEqual(Object.keys(directoryModes), ["plan", "ask", "accept_edits"]);
  for (const key of Object.keys(directoryModes)) assert.equal(directoryMode(key), key);
  assert.equal(directoryMode("full_access"), "accept_edits");
  assert.equal(directoryMode("approve_for_me"), "ask");
  assert.equal(directoryMode("unknown"), "ask");
});
