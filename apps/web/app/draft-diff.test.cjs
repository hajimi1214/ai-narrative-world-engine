const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const source = fs.readFileSync(path.join(__dirname, "draft-diff.ts"), "utf8");
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
const api = {};
vm.runInNewContext(code, { exports: api, Uint32Array, Math });
test("diff identifies preserved, removed and restored Chinese lines", () => {
  assert.deepEqual(JSON.parse(JSON.stringify(api.compareDraftLines("开头\n旧句\n结尾", "开头\n新句\n结尾"))), [
    { kind: "same", text: "开头" }, { kind: "removed", text: "旧句" }, { kind: "added", text: "新句" }, { kind: "same", text: "结尾" },
  ]);
});
test("diff normalizes CRLF and keeps identical text unchanged", () => {
  assert.equal(api.compareDraftLines("甲\r\n乙\r\n", "甲\n乙\n").every((line) => line.kind === "same"), true);
});
test("very long chapters use a bounded summary instead of quadratic alignment", () => {
  assert.deepEqual(JSON.parse(JSON.stringify(api.compareDraftLines("甲\n乙", "甲\n丙", 2))), [
    { kind: "removed", text: "旧版共 2 行" }, { kind: "added", text: "当前工作稿共 2 行；章节过长，按行数摘要对比" },
  ]);
});
