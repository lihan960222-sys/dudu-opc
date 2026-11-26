import assert from "node:assert/strict";
import test from "node:test";
import { captureManagedWindow, managedWindowId, requestManagedWindowExit } from "./managed-window.ts";

const id = "a973cb21423740769b4c081d67c18d52";
const owner = { kind: "browser", scopes: ["admin"], role: "owner", person_id: 1 };
const ineligible = [null, { kind: "loopback", scopes: ["admin"] }, { ...owner, kind: "device" },
  { ...owner, kind: "grant" }, { ...owner, kind: "desktop" }, { ...owner, role: "admin" },
  { ...owner, person_id: null }, { kind: "browser", scopes: ["admin"], role: null, person_id: null },
  { ...owner, role: undefined }, { ...owner, role: "member" }, { ...owner, scopes: ["chat"] }];

function page(url, storage = new Map()) {
  const timers = [];
  const state = { navigation: "preserved" };
  const result = {
    location: new URL(url),
    document: { title: "工作区" },
    sessionStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    history: { state, replaceState: (current, _, path) => {
      assert.equal(current, state);
      result.location = new URL(path, result.location);
    } },
    setTimeout: (fn, ms) => { assert.equal(ms, 15_000); timers.push(fn); return timers.length; },
    advance: () => { assert.ok(timers.length); timers.shift()(); },
    pending: () => timers.length,
    fetch: () => assert.fail("window control must not send a network request"),
    close: () => assert.fail("the native host owns the close choice"),
  };
  return result;
}

test("the signed-out login page pairs only its physical window, including after a full reload", async () => {
  const storage = new Map();
  const initial = page(`http://127.0.0.1:8000/login/?next=/chat/#dudu-managed=${id}`, storage);
  captureManagedWindow(initial);
  assert.equal(initial.location.href, "http://127.0.0.1:8000/login/?next=/chat/");
  assert.equal(initial.document.title, `Dudu [${id}]`);
  captureManagedWindow(initial);
  assert.equal(initial.pending(), 1);
  const reopened = page(initial.location.href, storage);
  captureManagedWindow(reopened);
  assert.equal(reopened.document.title, `Dudu [${id}]`);
  let checks = 0;
  for (const session of ineligible) {
    assert.equal(managedWindowId(reopened, session), null);
    await requestManagedWindowExit(reopened, session, async () => { checks++; return owner; });
  }
  assert.equal(checks, 0);
  reopened.advance();
  assert.equal(reopened.document.title, "工作区");
  assert.equal(reopened.pending(), 0);
  assert.equal(managedWindowId(reopened, owner), id);
});

test("blocked storage still supports this page and the marker preserves a route title update", () => {
  const tab = page(`http://127.0.0.1/#dudu-managed=${id}`);
  tab.sessionStorage.setItem = () => { throw new Error("storage disabled"); };
  const previous = Object.getOwnPropertyDescriptor(globalThis, "MutationObserver");
  const observation = {};
  try {
    globalThis.MutationObserver = class {
      constructor(callback) { observation.callback = callback; }
      observe(node) { observation.target = node; }
      disconnect() { observation.target = null; }
    };
    tab.document.head = {};
    captureManagedWindow(tab);
    assert.equal(managedWindowId(tab, owner), id);
    assert.equal(observation.target, tab.document.head);
    tab.document.title = "项目工作台";
    observation.callback();
    assert.equal(tab.document.title, `Dudu [${id}]`);
    tab.advance();
    assert.equal(tab.document.title, "项目工作台");
    assert.equal(observation.target, null);
  } finally {
    if (previous) Object.defineProperty(globalThis, "MutationObserver", previous);
    else delete globalThis.MutationObserver;
  }
});

test("ordinary tabs, remote sites, invalid tokens and other fragments do not activate the native window", async () => {
  for (const url of ["http://127.0.0.1/chat/", "http://127.0.0.1/#feishu-access", "http://127.0.0.1/#dudu-managed=abc",
    `http://192.168.1.2/#dudu-managed=${id}`, `http://localhost/#dudu-managed=${id}`,
    `http://127.0.0.1.example/#dudu-managed=${id}`, `https://127.0.0.1/#dudu-managed=${id}`,
    `http://127.0.0.1/#dudu-managed=${id}&other=1`]) {
    const tab = page(url);
    captureManagedWindow(tab);
    assert.equal(managedWindowId(tab, owner), null);
    await requestManagedWindowExit(tab, owner, () => assert.fail("no session check outside a managed window"));
    assert.equal(tab.location.href, new URL(url).href);
    assert.equal(tab.document.title, "工作区");
    assert.equal(tab.pending(), 0);
  }
  const remote = page(`https://dudu.example/#dudu-managed=${id}`, new Map([["dudu:managed-window", id]]));
  captureManagedWindow(remote);
  assert.equal(managedWindowId(remote, owner), null);
  assert.equal(remote.pending(), 0);
});

test("one marked native window handles both early and later exit requests without a second prompt", async () => {
  const tab = page(`http://127.0.0.1/#dudu-managed=${id}`);
  let checks = 0;
  const verify = async () => { checks++; return owner; };
  captureManagedWindow(tab);
  assert.equal(tab.document.title, `Dudu [${id}]`);
  const clicked = requestManagedWindowExit(tab, owner, verify);
  await requestManagedWindowExit(tab, owner, verify);
  assert.equal(tab.pending(), 1);
  assert.equal(tab.document.title, `Dudu [${id}]`);
  tab.advance();
  await clicked;
  assert.equal(tab.document.title, `Dudu close [${id}]`);
  await requestManagedWindowExit(tab, owner, verify);
  assert.equal(checks, 1);
  assert.equal(tab.pending(), 1);
  tab.advance();
  assert.equal(tab.document.title, "工作区");
  tab.document.title = "员工档案";
  await requestManagedWindowExit(tab, owner, verify);
  assert.equal(tab.document.title, `Dudu close [${id}]`);
  tab.document.title = "项目";
  tab.advance();
  assert.equal(tab.document.title, "项目");
});

test("an owner who signs out, changes role, or loses the server before exit cannot request a prompt", async () => {
  for (const result of [...ineligible, new Error("signed out")]) {
    const tab = page(`http://127.0.0.1/#dudu-managed=${id}`);
    captureManagedWindow(tab);
    let checks = 0;
    const clicked = requestManagedWindowExit(tab, owner, async () => {
      checks++;
      if (result instanceof Error) throw result;
      return result;
    });
    tab.advance();
    await clicked;
    assert.equal(checks, 1);
    assert.equal(tab.document.title, "工作区");
    assert.equal(tab.pending(), 0);
  }
});
