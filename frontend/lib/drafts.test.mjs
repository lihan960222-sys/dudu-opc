import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createChatDrafts, loadDraft, saveDraft } from './drafts.ts';

const memory = () => {
  const m = new Map();
  return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => m.set(k, v), removeItem: (k) => m.delete(k), size: () => m.size };
};

test('a draft is kept per conversation and cleared when emptied', () => {
  const s = memory();
  saveDraft('1', 'hello', s);
  saveDraft('2', 'other', s);
  assert.equal(loadDraft('1', s), 'hello');
  assert.equal(loadDraft('2', s), 'other');
  saveDraft('1', '   ', s);
  assert.equal(loadDraft('1', s), '');
  assert.equal(s.size(), 1);
});

test('no key or a throwing store never breaks the box', () => {
  const broken = { getItem() { throw new Error('x'); }, setItem() { throw new Error('x'); }, removeItem() { throw new Error('x'); } };
  assert.equal(loadDraft(null, memory()), '');
  assert.equal(loadDraft('1', broken), '');
  assert.doesNotThrow(() => saveDraft('1', 'a', broken));
  assert.doesNotThrow(() => saveDraft('1', '', broken));
});

test('switching chats, sending the first message and reopening preserve the right drafts', () => {
  const s = memory();
  const drafts = createChatDrafts(s);
  drafts.set('new', '你好');
  drafts.set('9', 'unfinished');
  assert.equal(drafts.get('8'), '');
  assert.equal(drafts.get('new'), '你好');
  drafts.moveSubmitted('new', '10', '你好');
  assert.equal(drafts.get('new'), '');
  assert.equal(drafts.get('10'), '你好'); // retry is possible if sending fails
  drafts.clearSubmitted('10', '你好');
  assert.equal(createChatDrafts(s).get('10'), '');
  assert.equal(createChatDrafts(s).get('new'), '');
  assert.equal(drafts.get('9'), 'unfinished');
});

test('a late send result preserves newer edits and drafts work with storage unavailable', () => {
  const drafts = createChatDrafts(null);
  drafts.set('new', 'first');
  drafts.set('new', 'later');
  drafts.moveSubmitted('new', '12', 'first');
  drafts.set('12', 'different');
  drafts.clearSubmitted('12', 'first');
  assert.equal(drafts.get('new'), 'later');
  assert.equal(drafts.get('12'), 'different');
});
