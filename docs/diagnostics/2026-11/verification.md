# 诊断验证记录

## 范围与环境

诊断基线为 `d2575d0`，分支 `main`，该提交时本地与 `origin/main` 同步。研究期间没有改动产品代码。文档变更完成后仓库 dirty 状态另行记录在交付说明。

没有访问作者数据库、真实小说正文、真实模型服务或密钥。隔离的 API 探针仅使用内存 SQLite、合成项目 ID、禁用 provider、空 key 与 dummy model 名。不得将这些探针理解为真实 provider 可用性测试。

## 本轮执行

### 已执行的隔离测试

- Writer、质量门、自动导演、作者约束、长篇基线、黄金规则、人物记忆、检索和研究模块的指定测试集，448 passed。首轮因临时覆盖 `AI_BASE_URL` 与默认设置断言冲突而有 2 项失败；保持 provider 禁用和空 key、把 URL 设回代码默认值后完整复跑 448 项通过。这是测试配置修正，不是产品代码失败。
- 直接解析 Writer Pydantic 输出模型，观察到 `prose=""` 被接受，且 `task_coverage`、`task_forbidden_hits` 自动默认为空列表。
- 隔离调用项目模型测试 API，provider 禁用且无 key 时返回 HTTP 500。测试没有联网调用 provider。
- 隔离保存模型设置，未配置 `AI_CREDENTIAL_MASTER_KEY` 时返回 400 与 `MODEL_CREDENTIAL_VAULT_NOT_CONFIGURED`。没有提供或打印任何凭据。

### 近期仓库回归参考

当前 `d2575d0` 在本任务之前已有完整 API 测试记录 `1373 passed, 41 skipped`，并通过 Web 生产构建。本轮没有重跑 60 个 API 测试文件的全集。上面的 448 项属于本轮选出的专项子集。此处明确区分前次全量记录与本轮实际执行结果。

### 可复核测试入口

- 当前模块函数式黄金测试运行方式在 [GOLDEN_NOVEL_BASELINE.md](../../../apps/api/tests/GOLDEN_NOVEL_BASELINE.md)。
- 模型网关隔离测试在 [test_phase1_model_gateway.py](../../../apps/api/tests/test_phase1_model_gateway.py)。
- Writer 契约测试在 [test_writer.py](../../../apps/api/tests/test_writer.py)。
- Critic、质量门和采用前提测试在 [test_quality_gate.py](../../../apps/api/tests/test_quality_gate.py)。
- 全自动导演与预算测试在 [test_auto_director.py](../../../apps/api/tests/test_auto_director.py)。
- 检索和研究测试在 [test_retrieval_index.py](../../../apps/api/tests/test_retrieval_index.py) 与 [test_research.py](../../../apps/api/tests/test_research.py)。

## 外部研究核验

- arXiv API 核对了 RAG、MemGPT、Generative Agents、LongWriter、MT-Bench/Judge、FActScore、G-Eval、LOT 和 WritingBench 的题名、提交日期与摘要。
- Crossref API 核对了 Plan-and-Write 与 CharacterEval 的 DOI/题名，以及 Narrative Engagement 的 DOI/出版信息。
- 本地 OSS 来源清单锁定 Pagekin、novelWriter、BookStack 和 RAGFlow 的上游 commit 与许可证。没有在本阶段下载、执行或复制其代码。
- ACL Anthology 与 Crossref 有个别请求返回限流或无法访问；这些结论以 arXiv/Crossref 已返回的元数据为准，不推断无法核验的全文细节。

## 未执行项目

- 没有真实模型、真实用户 key、token 计量缺失 provider 的外部端到端测试。Token 预算缺口根据接口默认 `usage={}` 与预算读取代码确认，仍需要 mock `usage={}` 的自动导演回归覆盖。
- 没有生产 PostgreSQL 或 pgvector 数据库。本地 PostgreSQL 专项测试在默认 SQLite 环境不会执行，因此其生产迁移、并发和 ANN 性能不由本轮背书。
- 没有跨 30 章或跨多卷的生成小说，也没有独立读者盲评。因此本轮没有测得人物感染力、阅读吸引力、中文文风接受度或 Critic-human 相关性。
- 未确认模型供应商服务条款、密钥主密钥备份策略、Windows 休眠/关机后的租约边界、长时间模型请求的真实成本波动。
