const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "finding-span.ts"), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
const api = {};
vm.runInNewContext(code, { exports: api, Number });

test("finding span converts Unicode code points to UTF-16 after supplementary characters", () => {
  const text = "灯塔🧭映出旧信";
  assert.deepEqual(JSON.parse(JSON.stringify(api.mapFindingSpan(text, 3, 5, "映出"))), { start: 4, end: 6 });
});

test("finding span refuses stale offsets and partial or invalid ranges", () => {
  assert.equal(api.mapFindingSpan("新版正文", 0, 2, "旧稿"), null);
  assert.equal(api.mapFindingSpan(undefined, 0, 2, "正文"), null);
  assert.equal(api.mapFindingSpan(null, 0, 2, "正文"), null);
  assert.equal(api.mapFindingSpan("正文", 1, 1, "文"), null);
  assert.equal(api.mapFindingSpan("正文", null, 1, "正"), null);
});

test("finding span handles the first code point and the end of the text", () => {
  assert.deepEqual(JSON.parse(JSON.stringify(api.mapFindingSpan("开头和结尾", 0, 2, "开头"))), { start: 0, end: 2 });
  assert.deepEqual(JSON.parse(JSON.stringify(api.mapFindingSpan("开头和结尾", 3, 5, "结尾"))), { start: 3, end: 5 });
});

test("finding span keeps newlines in exact prose offsets", () => {
  assert.deepEqual(JSON.parse(JSON.stringify(api.mapFindingSpan("第 1 段。\n\n某人的行动被审稿引用。\n结尾", 8, 14, "某人的行动被"))), { start: 8, end: 14 });
});
