import assert from "node:assert/strict";
import { test } from "node:test";
import { downloadStewardReply, presentStewardReply, stewardPreview, stewardTurnStatus } from "./steward-preview.ts";

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

test("the web download link serves every byte of the original Markdown and is then released", async (t) => {
  const prefix = '老板，我还没收到回执。\n> 原话\n```sh\nexit 7\n```\n';
  const original = prefix + "𠮷".repeat(1001 - Array.from(prefix).length);
  const saved = presentStewardReply(original);
  let clicked = false;
  let release;
  const link = { click() { clicked = true; } };
  t.after(() => { delete globalThis.window; delete globalThis.document; });
  globalThis.document = { createElement(tag) { assert.equal(tag, "a"); return link; } };
  globalThis.window = { setTimeout(fn) { release = fn; } };
  downloadStewardReply(saved.markdown, "大嘴完整回复");
  assert.ok(clicked);
  assert.equal(link.download, "大嘴完整回复.md");
  const response = await fetch(link.href);
  assert.equal(response.headers.get("content-type"), "text/markdown;charset=utf-8");
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), Buffer.from(original, "utf8"));
  release();
  await assert.rejects(fetch(link.href));
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
  assert.equal(stewardPreview("老板，你赶紧确认权限。"), "老板，请您确认权限。");
  assert.equal(stewardPreview("你希望我用哪份文档？"), "您希望我用哪份文档？");
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

test("a question addressed to the owner changes alongside inline and multiline verbatim evidence", () => {
  const evidence = '这一步返回“不能派工。你赶紧确认权限。”，字段是 `task_id`。';
  const blocks = '\n\n> 你给个编号。\n\n~~~~text\n给个编号。\n\n你赶紧确认权限。\n~~~~\n\n';
  const original = evidence + '给个任务编号，我再核对。' + blocks + '老板，你赶紧确认权限。';
  assert.deepEqual(presentStewardReply(original), {
    text: evidence + '您方便告诉我任务编号吗？我再核对。' + blocks + '老板，请您确认权限。', markdown: null,
  });
  const diagnostics = '\n\n运行记录：\n给个编号。\n你必须确认范围。';
  assert.equal(stewardPreview('给个编号。' + diagnostics), '您方便告诉我编号吗？' + diagnostics);
  const rawError = '报错原文：\n你赶紧确认权限。\n给个编号。';
  assert.equal(stewardPreview(rawError + '\n\n你直接给个文件名。'), rawError + '\n\n您方便告诉我文件名吗？');
  const cited = "员工说'第一句。你赶紧确认权限。'，文档为[原文](local.txt)。";
  assert.equal(stewardPreview(cited + '给个编号。'), cited + '您方便告诉我编号吗？');
  for (const size of [999, 1000, 1001]) {
    const tail = '。给个编号。';
    const source = '𠮷'.repeat(size - Array.from(tail).length) + tail;
    const saved = presentStewardReply(source);
    assert.ok(Array.from(saved.text).length <= 1000);
    assert.equal(saved.markdown, size > 1000 ? source : null);
    if (size <= 1000) assert.equal(saved.text, source.slice(0, -'给个编号。'.length) + '请问编号？');
  }
});
