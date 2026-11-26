import assert from "node:assert/strict";
import { test } from "node:test";
import { presentStewardReply, stewardPreview, stewardTurnStatus } from "./steward-preview.ts";

test("999 and 1000 Unicode characters fit directly, including supplementary Chinese and emoji", () => {
  for (const size of [999, 1000]) {
    const text = "甲" + "𠮷".repeat(size - 2) + "🙂";
    assert.deepEqual(presentStewardReply(text), { text, markdown: null });
  }
});

test("a 1001-character original gets a brief summary even if polite wording shortens it to 1000", () => {
  const opening = "你必须确认范围。";
  const text = opening + "𠮷".repeat(1001 - Array.from(opening).length);
  assert.deepEqual(presentStewardReply(text), { text: "请您确认范围。\n详见完整回复。", markdown: text });
});

test("steward failure takes responsibility and preserves the entire quoted diagnostic", () => {
  const detail = '> 你必须提供权限。\n```sh\nexit 7\n```';
  const full = stewardTurnStatus("failed", detail);
  assert.deepEqual(presentStewardReply(full), { text: '老板，这次我没办完。\n\n运行记录：\n' + detail, markdown: null });
  const long = stewardTurnStatus("failed", detail + "中".repeat(1000));
  assert.deepEqual(presentStewardReply(long), { text: "老板，这次我没办完。\n详见完整回复。", markdown: long });
  assert.match(stewardTurnStatus("interrupted"), /尚未核实/);
});

test("long replies show complete sentences", () => {
  const text = "文档已经更新。\n" + "具体内容".repeat(260);
  assert.equal(stewardPreview(text), "文档已经更新。\n详见完整回复。");
  assert.ok(Array.from(stewardPreview("😀".repeat(1001))).length < 150);
  assert.match(stewardPreview("𠮷".repeat(1001)), /^原文开头：𠮷+…\n详见完整回复。$/u);
});

test("owner requests are polite; evidence, quotes, and code remain exact", () => {
  assert.equal(stewardPreview("你得先确认权限。"), "请您先确认权限。");
  assert.equal(stewardPreview("请您先确认权限。"), "请您先确认权限。");
  assert.equal(stewardPreview("未写入。"), "未写入。");
  assert.equal(stewardPreview("你得知结果。"), "你得知结果。");
  const quoted = "员工原话：\n> 你必须提供权限。";
  assert.equal(stewardPreview(quoted), quoted);
  const code = "```txt\n你必须填写。\n```";
  assert.equal(stewardPreview(code), code);
});

test("the preview ends with a conclusion, never a dangling list number", () => {
  for (const detail of ["\n1. 第一步。\n2. ", " 1. 第一步。 2. ", "：1. 第一步。2. "]) {
    const text = "**尚未写入**。我会继续跟进。" + detail + "细节".repeat(510);
    assert.equal(stewardPreview(text), "尚未写入。我会继续跟进。\n详见完整回复。");
  }
});
