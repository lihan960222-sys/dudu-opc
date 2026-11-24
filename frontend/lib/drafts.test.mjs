import assert from 'node:assert/strict';
import { test } from 'node:test';
import { chatDraftKey, createChatDrafts, loadDraft, saveDraft } from './drafts.ts';

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

test('legacy plain text never automatically fills another employee or an existing conversation', () => {
  const s = memory();
  s.setItem('suparagent:draft:new', 'old greeting');
  s.setItem('suparagent:draft:7', 'already delivered');
  const drafts = createChatDrafts(s);
  assert.equal(drafts.get('7'), '');
  assert.equal(drafts.legacy('7', 'already delivered'), '');
  assert.equal(drafts.legacy('new:agent:8'), '');
  assert.equal(drafts.legacy('new'), 'old greeting');
  assert.equal(drafts.legacy('7', 'different user message'), 'already delivered');
  drafts.restore('7');
  assert.equal(createChatDrafts(s).get('7'), 'already delivered');
  assert.equal(drafts.legacy('7'), '');
  s.setItem('suparagent:draft:8', 'another accepted input');
  drafts.reconcile('8', 'another accepted input');
  assert.equal(drafts.legacy('8', 'a later turn'), '');
});

test('two views in the same tab see edits and acknowledgements immediately', () => {
  const s = memory(), first = createChatDrafts(s), second = createChatDrafts(s);
  first.set('7', 'original');
  let changes = 0;
  const stop = first.subscribe(() => changes++);
  second.set('7', 'second view');
  assert.equal(first.get('7'), 'second view');
  second.clearSubmitted('7', 'second view');
  assert.equal(first.get('7'), '');
  assert.ok(changes >= 2);
  stop();
});

test('each new recipient keeps its own manual draft or explicit prefill across navigation and refresh', () => {
  const s = memory(), d = createChatDrafts(s);
  const a = chatDraftKey(null, 4), b = chatDraftKey(null, 7), plain = chatDraftKey(null, null);
  d.set(a, 'unsent for A');
  saveDraft(b, 'requested starter', s);
  assert.equal(d.get(plain), '');
  d.moveSubmitted(a, chatDraftKey(32, 4), 'unsent for A');
  d.clearSubmitted('32', 'unsent for A');
  assert.equal(d.get(a), '');
  assert.equal(loadDraft(b, s), 'requested starter');
  assert.equal(chatDraftKey(32, null), '32');
});

test('refresh after the server accepted a message reconciles the pending send without clearing a later manual draft', () => {
  const s = memory(), d = createChatDrafts(s);
  d.set('8', '/review repeat');
  d.markSending('8', '/review repeat', 'repeat', 1000);
  d.reconcile('8', 'repeat', new Date(900).toISOString());
  assert.equal(d.get('8'), '/review repeat');
  d.reconcile('8', 'repeat', new Date(1100).toISOString());
  assert.equal(d.get('8'), '');
  d.set('8', 'repeat');
  d.reconcile('8', 'repeat', new Date(1200).toISOString());
  assert.equal(d.get('8'), 'repeat');
});
