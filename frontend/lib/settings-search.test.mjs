import assert from "node:assert/strict";
import { test } from "node:test";
import { localizedSettingsEntries, rankSettings, settingsSearchKeyAction } from "./settings-registry.ts";
import { settingsGeneralMessages } from "./locales/settings-general-extra.ts";

test("translated names and English keywords find the same setting and keep its anchor", () => {
  const entries = localizedSettingsEntries(key => settingsGeneralMessages.zh[key] ?? key);
  for (const query of ["更新", "check for updates"]) {
    const [first] = rankSettings(query, entries);
    assert.equal(first.entry.title, "检查更新");
    assert.equal(first.entry.anchorId, "setting-general-updates");
  }
  assert.ok(entries.every(entry => !entry.title.startsWith("settingsSearch.") && !entry.section.startsWith("settingsSection.")));
});

test("settings search supports wrapped arrow navigation and Home/End", () => {
  assert.deepEqual(settingsSearchKeyAction("ArrowDown", 2, 3), { type: "move", active: 0 });
  assert.deepEqual(settingsSearchKeyAction("ArrowUp", 0, 3), { type: "move", active: 2 });
  assert.deepEqual(settingsSearchKeyAction("ArrowDown", 0, 0), { type: "move", active: 0 });
  assert.deepEqual(settingsSearchKeyAction("Home", 2, 4), { type: "move", active: 0 });
  assert.deepEqual(settingsSearchKeyAction("End", 0, 4), { type: "move", active: 3 });
});

test("settings search Enter selects, Escape clears, and other keys pass through", () => {
  assert.deepEqual(settingsSearchKeyAction("Enter", 1, 3), { type: "select" });
  assert.deepEqual(settingsSearchKeyAction("Escape", 1, 3), { type: "clear" });
  assert.equal(settingsSearchKeyAction("a", 0, 3), null);
});
