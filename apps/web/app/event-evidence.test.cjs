const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "event-evidence.ts"), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
const findingCode = ts.transpileModule(fs.readFileSync(path.join(__dirname, "finding-span.ts"), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
const findingApi = {};
vm.runInNewContext(findingCode, { exports: findingApi, Number });
const api = {};
vm.runInNewContext(code, {
  exports: api,
  require: (id) => id === "./finding-span" ? findingApi : require(id),
});

test("confirmed evidence is actionable only for the matching draft and exact quote", () => {
  const prose = "她把铜钥匙攥进掌心，转身离开。";
  const review = { confirmed: true, start_offset: 1, end_offset: 9, excerpt: "把铜钥匙攥进掌心" };
  assert.deepEqual(JSON.parse(JSON.stringify(api.assessEventEvidence(prose, review, true))), {
    status: "CONFIRMED", span: { start: 1, end: 9 },
  });
  assert.deepEqual(JSON.parse(JSON.stringify(api.assessEventEvidence("新正文", review, true))), {
    status: "INVALID_EVIDENCE", span: null,
  });
});

test("unconfirmed and stale event reviews never authorize a text jump", () => {
  const review = { confirmed: false, start_offset: 0, end_offset: 3, excerpt: "钥匙" };
  assert.equal(api.assessEventEvidence("拿到钥匙", review, true).status, "NOT_CONFIRMED");
  assert.deepEqual(JSON.parse(JSON.stringify(api.assessEventEvidence("拿到钥匙", { ...review, confirmed: true }, false))), {
    status: "STALE", span: null,
  });
});

test("event evidence maps code-point offsets after supplementary characters", () => {
  const prose = "她举起🧭，把钥匙交给守门人。";
  const excerpt = "把钥匙交给守门人";
  const start = Array.from(prose.slice(0, prose.indexOf(excerpt))).length;
  const result = api.assessEventEvidence(prose, {
    confirmed: true,
    start_offset: start,
    end_offset: start + Array.from(excerpt).length,
    excerpt,
  }, true);
  assert.equal(result.status, "CONFIRMED");
  assert.equal(prose.slice(result.span.start, result.span.end), excerpt);
});
