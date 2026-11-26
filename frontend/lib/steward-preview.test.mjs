import assert from "node:assert/strict";
import { test } from "node:test";
import { stewardPreview } from "./steward-preview.ts";

test("short and boundary replies are kept intact", () => {
  assert.equal(stewardPreview("😀".repeat(200)), "😀".repeat(200));
});

test("long replies show complete sentences", () => {
  const text = "文档已经更新。\n" + "具体内容".repeat(100);
  assert.equal(stewardPreview(text), "文档已经更新。\n详见完整回复。");
  assert.ok(Array.from(stewardPreview("😀".repeat(201))).length <= 200);
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
    const text = "**尚未写入**。我会继续跟进。" + detail + "细节".repeat(120);
    assert.equal(stewardPreview(text), "尚未写入。我会继续跟进。\n详见完整回复。");
  }
});
