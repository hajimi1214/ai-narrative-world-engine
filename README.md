# AI Narrative World Engine

**本地中文长篇小说创作工作台**，面向单作者、Windows 10/11 和本机 SQLite。当前候选版本为 **0.4.0-rc.1**。它把规划、章节写作、审核、版本历史、卷快照、恢复和导出连成一条作者可接管的流程。

> 这是发布候选，不是 GitHub Release。生成模型需要作者自行配置，模型调用可能产生费用。小说质量仍由作者判断。

## 作者快速开始

环境要求为 Python 3.12、Node.js 20 LTS、pnpm 10 和 uv 0.11 或兼容版本。

```powershell
py -3.12 -m pip install uv
uv sync --locked --extra test --python 3.12
cd apps/web
pnpm install --frozen-lockfile
cd ../..
```

运行 `start-platform.bat`，或在 PowerShell 执行 `.\start-platform.ps1`。浏览器会打开 `http://127.0.0.1:3000`。默认 AI 提供方为禁用状态，不会在首次启动时发起模型调用。

需要在项目设置页保存模型密钥时，先运行一次 `.\scripts\initialize-local-secrets.ps1`。它会在被 Git 忽略的 `.env` 中创建本机加密主密钥，不会在终端显示密钥。该主密钥必须保管好，备份不会包含它。更多操作步骤、暂停恢复、封卷和导出说明见[中文作者使用指南](docs/USER_GUIDE_ZH.md)。

## 工作方式

- 从灵感、大纲、已有正文、世界观或空白入口创建作品
- 选择作者主导卷级创作、自动导演或仅整本规划
- 查看并修改书级方向、章节任务、人物资料和世界事实
- 生成正文草稿，检查审核证据，编辑或生成修复稿，再由作者批准或采用
- 在 AI 实况中查看检查点、预算状态、重试和作者待处理事项
- 备份作品，恢复为副本，导出 TXT 或 Markdown 成稿

作者主导卷级模式按滚动窗口推进。自动跨卷默认关闭。启用后，系统会先封存当前卷并保存连续性快照，再以同一自动运行的剩余预算推进。失败、预算耗尽或资料不足时会停在可恢复状态。

## 质量与证据边界

Writer、正式世界分支、章节版本、审核证据、卷快照和恢复检查点都有确定性测试。它们能支持状态一致性和恢复行为，不能保证中文语义判断绝对正确，也不能把 Critic 的 PASS 当成文学质量认证。

第四阶段冻结了 A/B/C 对照方案与中文读者评分量表。当前没有真实模型生成的实验语料、独立读者评分或作者修改负担记录，文学收益状态为 **NOT_EVALUATED**。第五阶段的人物声线设置和写作提示有工程链路验证，尚无读者效果证据。完整版本边界和已知限制见[0.4.0-rc.1 验收记录](docs/releases/0.4.0-rc.1.md)。

中文文学评分标准、冻结的 A/B/C 试点任务、盲评包生成和评分汇总说明见[研究者评测协议](research/evaluation/README.md)。目前协议和工具已经测试，真实模型输出和读者评分尚未收集。

## 验证

```powershell
uv run --locked python -m pytest -q apps/api/tests
cd apps/web
pnpm test
pnpm exec tsc --noEmit --incremental false
pnpm build
```

GitHub Actions 在 Windows runner 上执行 API 全量测试、Web 工具测试、类型检查和生产构建。依赖 PostgreSQL 或外部服务的条件测试会按环境跳过，不能视为通过。测试不得使用真实小说数据库或真实 API 密钥。

## 数据与安全

- SQLite 默认位于项目根目录的 `narrative.db`。启动迁移前会保留时间戳备份。
- 项目备份可能包含完整小说、世界观和角色资料，请按私人创作内容保管。
- 项目备份不包含 API 密钥或 `AI_CREDENTIAL_MASTER_KEY`。恢复加密的项目密钥需要原主密钥。
- 不要把 `.env`、数据库、小说、备份或密钥提交到 Git。
- PostgreSQL Compose 服务只绑定到本机地址，并要求通过环境变量提供非空密码。

安全问题请遵循 [SECURITY.md](SECURITY.md)。贡献和测试约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。项目代码采用 [MIT License](LICENSE)，依赖继续受其各自许可证约束。

## 阶段文档

- [技术与文学质量诊断](docs/diagnostics/2026-11/README.md)
- [第四阶段中文长篇文学质量基线](docs/diagnostics/2026-11/phase-04-literary-evaluation.md)
- [第五阶段文学优化记录](docs/diagnostics/2026-11/phase-05-literary-optimization.md)
- [用户指南](docs/USER_GUIDE_ZH.md)
- [发布候选验收报告](docs/releases/0.4.0-rc.1.md)

## 许可证

MIT。仓库内没有复制第三方项目代码。开源依赖继续受各自许可证约束。
