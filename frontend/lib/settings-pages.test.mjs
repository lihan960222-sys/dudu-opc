import assert from "node:assert/strict";
import test from "node:test";
import { translate } from "./i18n.ts";
import { settingsPageMessages, settingsTerm } from "./locales/settings-pages.ts";

test("settings preserve translated labels, placeholders and technical identifiers", () => {
  const { en, zh } = settingsPageMessages;
  assert.deepEqual(Object.keys(en).sort(), Object.keys(zh).sort());
  const slots = value => [...value.matchAll(/\{(\w+)\}/g)].map(match => match[1]).sort();
  for (const key of Object.keys(en)) assert.deepEqual(slots(en[key]), slots(zh[key]), key);
  const t = key => translate("zh", key);
  assert.equal(settingsTerm(t,"Full access"), "完整访问");
  assert.equal(settingsTerm(t,"API key"), "API 密钥");
  assert.equal(settingsTerm(t,"https://example.invalid/v1"), "https://example.invalid/v1");
  assert.equal(translate("zh", "settingsPage.savedGrant", {name:"员工 A"}), "已保存 员工 A 的 MuAPI 权限。");
  assert.equal(translate("en", "settingsPage.joinedDate", {date:"2026-01-01"}), "Joined 2026-01-01");
});
