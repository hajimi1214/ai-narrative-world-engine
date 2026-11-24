const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

const web = "http://127.0.0.1:3182";
const api = "http://127.0.0.1:8182";
const widths = [320, 390, 768, 1024, 1440, 1920];
const outputPath = path.join(__dirname, "phase-05-branch-cognition-2026-11-24.json");

async function main() {
  const fixture = JSON.parse(await fs.readFile(path.join(__dirname, "release-0.3.0-fixtures.json"), "utf8"));
  const browser = await chromium.launch({
    executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
    headless: true,
  });
  const report = {
    status: "failed",
    date: "2026-11-24",
    data: "synthetic project and isolated SQLite fixture",
    model_provider: "disabled",
    viewports_css_px: widths,
    pages: {},
    page_errors: [],
  };
  try {
    const context = await browser.newContext({
      viewport: { width: 390, height: 900 },
      serviceWorkers: "block",
      reducedMotion: "reduce",
    });
    await context.route("**/*", async (route) => {
      const origin = new URL(route.request().url()).origin;
      if (origin !== web && origin !== api) return route.abort();
      return route.continue();
    });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    page.on("pageerror", (error) => report.page_errors.push(error.message));
    await page.addInitScript(({ fixture, api }) => {
      window.__fixture = fixture;
      window.__api = api;
    }, { fixture, api });

    async function checkWidths(name, mobileNavigation = false) {
      report.pages[name] = [];
      for (const width of widths) {
        await page.setViewportSize({ width, height: 900 });
        const dimensions = await page.evaluate(() => ({
          viewport: innerWidth,
          document: document.documentElement.scrollWidth,
          body: document.body.scrollWidth,
        }));
        assert.ok(dimensions.document <= width && dimensions.body <= width, name + " overflow at " + width + "px");
        if (mobileNavigation && width <= 768) {
          const open = page.getByRole("button", { name: "打开导航" });
          await open.click();
          await page.getByRole("button", { name: "关闭导航" }).waitFor();
          await page.keyboard.press("Escape");
          assert.ok(await open.evaluate((element) => element === document.activeElement), name + " navigation focus was not restored");
        }
        report.pages[name].push(dimensions);
      }
    }

    await page.goto(web, { waitUntil: "domcontentloaded" });
    await page.getByRole("heading", { name: "小说库", exact: true }).waitFor();
    await checkWidths("library");

    await page.getByRole("button", { name: "开始新小说" }).first().click();
    await page.getByLabel("灵感 / 故事前提").waitFor();
    await checkWidths("new-book-wizard");

    await page.goto(web + "/projects/" + fixture.project, { waitUntil: "domcontentloaded" });
    await page.locator(".breadcrumb strong").waitFor();
    await checkWidths("project-navigation", true);

    await page.goto(web + "/projects/" + fixture.project + "/chapters", { waitUntil: "domcontentloaded" });
    await page.locator(".chapter-workspace").waitFor();
    await page.getByRole("button", { name: "编辑正文" }).click();
    const editorCount = await page.locator("textarea, [contenteditable='true']").count();
    assert.ok(editorCount > 0, "chapter reader/editor did not expose an editing control");
    report.chapter_editor_controls = editorCount;
    await checkWidths("chapter-reader-editor", true);

    assert.deepEqual(report.page_errors, []);
    report.status = "passed";
  } catch (error) {
    report.error = error.message;
    process.exitCode = 1;
  } finally {
    await browser.close();
    await fs.writeFile(outputPath, JSON.stringify(report, null, 2) + "\n", "utf8");
    console.log(JSON.stringify({
      status: report.status,
      pages: Object.keys(report.pages),
      widths: report.viewports_css_px,
      chapter_editor_controls: report.chapter_editor_controls,
      page_errors: report.page_errors,
      error: report.error,
    }));
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
