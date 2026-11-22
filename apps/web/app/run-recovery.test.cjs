const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

const source = fs.readFileSync(path.join(__dirname, "run-recovery.ts"), "utf8");
const code = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS },
}).outputText;
const recovery = {};
vm.runInNewContext(code, { exports: recovery, URLSearchParams });

test("links retain the exact run and keep query delimiters inside its value", () => {
  const id = "run & volume=other?";
  const volume = new URL(
    recovery.directorHref("project", { id, run_mode: "AUTHOR_GUIDED_VOLUME" }),
    "http://localhost",
  );
  assert.equal(volume.pathname, "/projects/project/director/volume");
  assert.deepEqual([...volume.searchParams.entries()], [["run_id", id]]);
  const auto = new URL(
    recovery.directorHref("project", { id, run_mode: "FULL_AUTO" }),
    "http://localhost",
  );
  assert.equal(auto.pathname, "/projects/project/director/auto");
  assert.equal(auto.searchParams.get("run_id"), id);
});

test("an automatically scheduled retry is distinct from a historical failed run", () => {
  assert.equal(
    recovery.recoveryLabel({
      id: "scheduled",
      status: "FAILED",
      recovery: { state: "WAITING_RETRY" },
    }),
    "等待自动重试",
  );
  assert.equal(
    recovery.recoveryLabel({ id: "unscheduled", status: "FAILED" }),
    "需要作者处理",
  );
  assert.equal(
    recovery.recoveryLabel({
      id: "restarted",
      status: "RUNNING",
      recovery: { state: "QUEUED" },
    }),
    "等待本地服务接手",
  );
});

test("budget and completed window pauses explain why work stopped", () => {
  assert.equal(
    recovery.recoveryLabel({
      id: "budget",
      status: "PAUSED",
      pause_reason: "TOKEN_BUDGET_EXCEEDED",
    }),
    "已到本次预算上限",
  );
  assert.equal(
    recovery.recoveryLabel({
      id: "window",
      status: "PAUSED",
      pause_reason: "WINDOW_COMPLETE",
    }),
    "本窗口已完成",
  );
  assert.equal(
    recovery.recoveryLabel({ id: "manual", status: "PAUSED" }),
    "已暂停",
  );
});

test("a direction awaiting approval needs an explicit choice before resuming", () => {
  const run = {
    id: "book-direction",
    status: "PAUSED",
    current_stage: "DIRECTION_SELECTION",
    recovery: { state: "PAUSED" },
  };
  assert.equal(recovery.recoveryState(run), "NEEDS_AUTHOR");
  assert.equal(recovery.recoveryLabel(run), "等待作者确认方向");
});

test("saved UTC times agree with explicit ISO offsets and invalid values are hidden", () => {
  assert.equal(
    recovery.recoveryTime("2026-11-21T09:04:36"),
    recovery.recoveryTime("2026-11-21T17:04:36+08:00"),
  );
  assert.equal(recovery.recoveryTime("invalid"), undefined);
});

test("remaining budgets include committed chapter progress and uncertain token reserves", () => {
  const budget = recovery.runBudgetSummary({
    id: "resumed",
    settings: {
      operational_run_chapter_budget: 5,
      operational_token_budget: 10000,
    },
    context: { processed_this_run: 2, generated_this_run: 1 },
    token_usage: { total_tokens: 3000 },
    usage_metrics: { total_tokens: 3500 },
    usage_summary: { book: { total_tokens: 4000 } },
    recovery: { reserved_tokens: 500 },
  });
  assert.deepEqual(JSON.parse(JSON.stringify(budget)), {
    chapter_budget: 5,
    chapters_remaining: 3,
    token_budget: 10000,
    tokens_remaining: 5500,
    tokens_used: 4000,
    tokens_reserved: 500,
  });
});

test("unconfigured budgets stay unspecified instead of showing false zero remaining", () => {
  const budget = recovery.runBudgetSummary({ id: "unlimited" });
  assert.equal(budget.chapter_budget, null);
  assert.equal(budget.chapters_remaining, null);
  assert.equal(budget.token_budget, null);
  assert.equal(budget.tokens_remaining, null);
});
