const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const ts = require("typescript");

const compiled = ts.transpileModule(fs.readFileSync(`${__dirname}/live-poller.ts`, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
const flush = async () => { for (let index = 0; index < 8; index += 1) await Promise.resolve(); };

function harness({ rejectOnAbort = false } = {}) {
  let now = 1000; let nextId = 0;
  const jobs = new Map(); const requests = []; const states = []; const exported = {};
  vm.runInNewContext(compiled, {
    exports: exported, AbortController, Math, Error, Date: { now: () => now },
    setTimeout: (callback, delay) => { const id = ++nextId; jobs.set(id, { at: now + delay, callback }); return id; },
    clearTimeout: (id) => jobs.delete(id),
  });
  const poller = exported.createLivePoller({
    intervalMs: 100, maxBackoffMs: 800, timeoutMs: 500,
    onState: (state) => states.push({ ...state }),
    load: (signal) => new Promise((resolve, reject) => {
      requests.push({ signal, resolve, reject });
      if (rejectOnAbort) signal.addEventListener("abort", () => reject(new Error("ABORTED")), { once: true });
    }),
  });
  async function advance(ms) {
    const target = now + ms;
    for (let iteration = 0; iteration < 100; iteration += 1) {
      const next = [...jobs].filter(([, job]) => job.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      now = next[1].at; jobs.delete(next[0]); next[1].callback(); await flush();
    }
    now = target; await flush();
  }
  return { poller, requests, states, jobs, advance, last: () => states.at(-1) };
}

test("serial polling coalesces refreshes and waits for completion", async () => {
  const h = harness();
  assert.equal(h.requests.length, 0);
  h.poller.setPaused(false);
  h.poller.refresh(); h.poller.refresh(); h.poller.refresh();
  assert.equal(h.requests.length, 1);
  h.requests[0].resolve(); await flush(); await h.advance(0);
  assert.equal(h.requests.length, 2);
  h.requests[1].resolve(); await flush(); await h.advance(99);
  assert.equal(h.requests.length, 2);
  await h.advance(1);
  assert.equal(h.requests.length, 3);
  h.poller.dispose();
});

test("failures back off, retain the last update, and reset after success", async () => {
  const h = harness(); h.poller.setPaused(false);
  h.requests[0].resolve(); await flush();
  const previousUpdate = h.last().lastUpdatedAt;
  await h.advance(100); h.requests[1].reject(new Error("OFFLINE")); await flush();
  assert.equal(h.last().status, "offline"); assert.equal(h.last().lastUpdatedAt, previousUpdate);
  assert.equal(h.last().retryAt, 1300);
  await h.advance(199); assert.equal(h.requests.length, 2);
  await h.advance(1); h.requests[2].reject(new Error("OFFLINE")); await flush();
  assert.equal(h.last().retryAt, 1700);
  h.poller.refresh(); assert.equal(h.requests.length, 4);
  h.requests[3].resolve(); await flush();
  assert.equal(h.last().failures, 0); assert.equal(h.last().status, "ready");
  await h.advance(100); assert.equal(h.requests.length, 5);
  h.poller.dispose();
});

test("hidden tabs abort and resume without overlapping, disposal ignores late results", async () => {
  const h = harness(); h.poller.setPaused(false);
  h.poller.setPaused(true); assert.equal(h.requests[0].signal.aborted, true);
  h.poller.setPaused(false); assert.equal(h.requests.length, 1);
  h.requests[0].resolve(); await flush(); await h.advance(0);
  assert.equal(h.requests.length, 2);
  assert.equal(h.states.some((state) => state.status === "offline"), false);
  h.poller.setPaused(true); h.requests[1].resolve(); await flush(); await h.advance(5000);
  assert.equal(h.requests.length, 2);
  h.poller.setPaused(false); const stateCount = h.states.length;
  h.poller.dispose(); assert.equal(h.requests[2].signal.aborted, true);
  h.requests[2].resolve(); await flush(); await h.advance(5000);
  assert.equal(h.states.length, stateCount); assert.equal(h.jobs.size, 0);
});

test("timed out requests are cancelled before a retry is scheduled", async () => {
  const h = harness({ rejectOnAbort: true }); h.poller.setPaused(false);
  await h.advance(500);
  assert.equal(h.requests[0].signal.aborted, true); assert.equal(h.last().status, "offline");
  await h.advance(200); assert.equal(h.requests.length, 2);
  h.poller.dispose();
});
