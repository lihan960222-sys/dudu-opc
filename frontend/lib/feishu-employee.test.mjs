import assert from "node:assert/strict";
import { test } from "node:test";
import { assignedContact, feishuEntry } from "./feishu-employee.ts";

test("employees see their assignments, including the legacy default lead", () => {
  const contact = { chat_id: "oc_demo", peer_open_id: "ou_demo", agent_id: null };
  assert.equal(assignedContact(contact, 1, true), true);
  assert.equal(assignedContact(contact, 2, false), false);
  contact.agent_id = 2;
  assert.equal(assignedContact(contact, 1, true), false);
  assert.equal(assignedContact(contact, 2, false), true);
  assert.equal(assignedContact(contact, 3, false), false);
});

test("private tools open only from their explicit link, old anchor or OAuth return", () => {
  const entry = (query, hash = "", returning = null) => feishuEntry(new URLSearchParams(query), hash, returning);
  assert.deepEqual(entry("section=host"), { requested: false, employee: "" });
  assert.deepEqual(entry("employee=3"), { requested: false, employee: "" });
  assert.deepEqual(entry("section=host", "#feishu-access"), { requested: true, employee: "" });
  assert.deepEqual(entry("capability=feishu&employee=3"), { requested: true, employee: "3" });
  assert.deepEqual(entry("section=host&feishu_oauth=connected", "", "3"), { requested: true, employee: "3" });
  assert.deepEqual(entry("section=host&feishu_oauth=error", "", "new"), { requested: true, employee: "" });
  assert.deepEqual(entry("feishu_oauth=connected&employee=4", "", "3"), { requested: true, employee: "4" });
  assert.deepEqual(entry("feishu_oauth=connected&employee=4"), { requested: true, employee: "4" });
});
