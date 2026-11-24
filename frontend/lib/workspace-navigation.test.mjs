import assert from "node:assert/strict";
import test from "node:test";
import { homeDestination, workspaceHref, workspaceSection, createdThreadHref, isStewardChatEntry } from "./workspace-navigation.ts";

test("old bookmarks and Feishu callbacks open the right workspace view", () => {
  assert.equal(homeDestination("", "", true), "/chat/?section=people");
  assert.equal(homeDestination("thread=32", "", true), "/chat/?thread=32");
  assert.equal(homeDestination("section=host&employee=7", "#feishu-access", true), "/chat/?section=people&employee=7&capability=feishu#feishu-access");
  assert.equal(homeDestination("feishu_oauth=ok&employee=7", "", true), "/chat/?feishu_oauth=ok&employee=7&section=people");
  assert.equal(homeDestination("section=host&employee=7&thread=32", "", false), "/chat/?thread=32");
  assert.equal(workspaceSection("view=agents", true), "people");
  assert.equal(workspaceSection("section=people&employee=7", false), "chat");
  assert.equal(workspaceSection("section=missing", true), "chat");
});

test("the same chat or room survives section changes without inheriting an employee editor or overlay", () => {
  assert.equal(workspaceHref("thread=32&view=team", "people", 7), "/chat/?thread=32&section=people&employee=7");
  assert.equal(workspaceHref("thread=32&section=people&employee=8&tools=1", "chat"), "/chat/?thread=32");
  assert.equal(workspaceHref("room=19&section=host", "projects"), "/chat/?room=19&section=projects");
  assert.equal(workspaceHref("section=people&employee=7", "projects", 7), "/chat/?section=projects&employee=7");
  assert.equal(workspaceHref("agent=7&capability=feishu", "people", "new"), "/chat/?agent=7&section=people&employee=new");
  assert.equal(workspaceHref("agent=7", "people", 7, true), "/chat/?agent=7&section=people&employee=7&tools=1");
});

test("a first reply arriving while looking at an employee preserves that open profile", () => {
  assert.equal(createdThreadHref("agent=4&section=people&employee=7&tools=1", 31), "/chat/?section=people&employee=7&tools=1&thread=31");
  assert.equal(createdThreadHref("agent=4", 31), "/chat/?thread=31");
});

test("only an unaddressed owner chat defaults to the steward", () => {
  assert.equal(isStewardChatEntry(""), true);
  assert.equal(isStewardChatEntry("section=chat"), true);
  for (const search of ["thread=31", "room=2", "agent=8", "plain=1", "engine=codex", "panel=skills", "view=team", "section=people"]) {
    assert.equal(isStewardChatEntry(search), false, search);
  }
  assert.equal(workspaceHref("plain=1&section=people&employee=7", "chat"), "/chat/?plain=1");
});
