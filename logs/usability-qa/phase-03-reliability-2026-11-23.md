# Phase 03 Responsive QA

Date: 2026-11-23

Environment: isolated SQLite database `.pytest_cache/phase03-browser-qa.db`, synthetic project/run/chapter data, API at `127.0.0.1:8123`, Next.js at `127.0.0.1:4300`, and disabled AI provider. No author database, manuscript, credentials, or paid model call was used.

Pages checked: library home, new-book wizard, project dashboard, automatic director with a token-budget pause, volume director with a token-budget pause, chapter workbench, and planning workbench.

| Viewport width | Document width | Result |
| ---: | ---: | --- |
| 320 | 320 | Pass |
| 390 | 390 | Pass |
| 768 | 768 | Pass |
| 1024 | 1024 | Pass |
| 1440 | 1440 | Pass |
| 1920 | 1920 | Pass |

The 320-pixel project layout exposes the mobile navigation control and opens the project navigation overlay. The automatic director budget pause visibly showed confirmed token usage, one unknown-usage call, 300 reserved tokens, cost `UNKNOWN`, and the increase-budget action. No page-level horizontal overflow was observed.

The browser console reported the existing `/favicon.ico` 404. This was recorded and left outside the reliability scope.
