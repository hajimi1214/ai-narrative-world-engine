const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

const source = fs.readFileSync(path.join(__dirname, "run-recovery.ts"), "utf8");
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
const recovery = {};
vm.runInNewContext(code, { exports: recovery, URLSearchParams });

test("links retain the exact run and keep query delimiters inside its value", () => {
  const id = "run & volume=other?";
  const volume = new URL(recovery.directorHref("project", { id, run_mode: "AUTHOR_GUIDED_VOLUME" }), "http://localhost");
  assert.equal(volume.pathname, "/projects/project/director/volume");
  assert.deepEqual([...volume.searchParams.entries()], [["run_id", id]]);
  const auto = new URL(recovery.directorHref("project", { id, run_mode: "FULL_AUTO" }), "http://localhost");
  assert.equal(auto.pathname, "/projects/project/director/auto");
  assert.equal(auto.searchParams.get("run_id"), id);
});

test("an automatically scheduled retry is distinct from a historical failed run", () => {
  assert.equal(recovery.recoveryLabel({ id: "scheduled", status: "FAILED", recovery: { state: "WAITING_RETRY" } }), "等待自动重试");
  assert.equal(recovery.recoveryLabel({ id: "unscheduled", status: "FAILED" }), "需要作者处理");
});

test("budget and completed window pauses explain why work stopped", () => {
  assert.equal(recovery.recoveryLabel({ id: "budget", status: "PAUSED", pause_reason: "TOKEN_BUDGET_EXCEEDED" }), "已到本次预算上限");
  assert.equal(recovery.recoveryLabel({ id: "window", status: "PAUSED", pause_reason: "WINDOW_COMPLETE" }), "本窗口已完成");
  assert.equal(recovery.recoveryLabel({ id: "manual", status: "PAUSED" }), "已暂停");
});

test("a direction awaiting approval needs an explicit choice before resuming", () => {
  const run = { id: "book-direction", status: "PAUSED", current_stage: "DIRECTION_SELECTION", recovery: { state: "PAUSED" } };
  assert.equal(recovery.recoveryState(run), "NEEDS_AUTHOR");
  assert.equal(recovery.recoveryLabel(run), "等待作者确认方向");
});

test("saved UTC times agree with explicit ISO offsets and invalid values are hidden", () => {
  assert.equal(recovery.recoveryTime("2026-11-21T09:04:36"), recovery.recoveryTime("2026-11-21T17:04:36+08:00"));
  assert.equal(recovery.recoveryTime("invalid"), undefined);
});
