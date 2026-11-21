# 本地作者工作台可用性升级

## SPEC

目的：作者能创建小说、按卷推进、修改正文、重新审核、导出作品并完整恢复资料。

[ASSUMPTION: 本轮继续面向本机单用户；使用现有模型配置，不新增云部署和账号体系。]
[ASSUMPTION: 人工保存产生新的工作稿版本，原版本保留；修改后必须重新审核才能批准为正式稿。]
[ASSUMPTION: 首批成稿导出提供 UTF-8 TXT 和 Markdown，支持全书和单卷。]

使用场景：

- 作为作者，我希望填写创作方向后开始卷级创作，以便逐卷控制故事。
- 作为作者，我希望修改正文并保留历史，以便安全改稿和恢复旧文。
- 作为作者，我希望下载成稿和完整备份，以便阅读、投稿和迁移电脑。

要求：

1. 所有新书入口明确选择作者主导、自动生产或仅规划；作者主导为默认推荐。
2. 创建前验证输入；后续失败重试复用已创建项目；保存已测试的模型连接。
3. 必需的语义审核失败不能获得 PASS，不能自动采用；重试预算按实际检查点次数计数。
4. 作者资料备份包含书籍/卷合同、窗口、连续性快照、伏笔、指导和完结建议；恢复为新项目并排除凭据。
5. 改稿保留原版本，拒绝过期编辑，保护封存卷并使旧质量批准失效。
6. 成稿按章序导出，明确区分已保存工作稿和质量批准稿，不导出未采用的失败候选。

验收场景：

- Given 默认作者向导，When 提交有效信息，Then 不因默认预算产生 422，连接配置传入项目。
- Given 作者卷级项目，When 导出再恢复，Then 权威作者状态和内部引用完整且原项目不变。
- Given 必需审核连续返回无效内容，When 审核结束，Then 保留失败原因且不能批准或自动采用。
- Given 已批准正文，When 作者保存修改，Then 创建新版本、旧正文仍在历史、质量批准失效。
- Given 编辑期间当前版本改变或卷已封存，When 保存，Then 返回冲突且不覆盖正文。
- Given 多章已保存正文，When 下载全书或指定卷，Then 文件顺序正确且包含对应正文。

## PLAN

沿用 FastAPI、SQLAlchemy、SQLite、Next.js；领域校验放服务层，HTTP 层负责项目归属与错误转换。

- 备份：补齐作者表和版本历史，校验引用，分阶段恢复循环外键，重建新项目指纹。
- 审核/编排：严格审核失败、精确检查点重试、修正分阶段执行回归。
- 改稿：新增人工稿来源标记，复用 Writer 指纹、版本和采用生命周期，不从正文修改世界事实。
- API：POST `/projects/{id}/chapters/{chapter_id}/writer/edit`；GET `/projects/{id}/manuscript?format=txt|md&volume_id=...&approved_only=false|true`。
- 前端：整合新书向导，修正当前卷选择，章节页增加改稿、取消、保存和成稿下载。
- 启动：检查端口归属、等待就绪，保留现有数据库并备份后准备。

验证：内存数据库回归（不调用外部模型）、备份往返、改稿失效/冲突/封存保护、成稿范围隔离、完整后端测试、前端类型与生产构建。浏览器使用独立测试库验证新增交互。

## TASKS

1. 备份与恢复：修改 backup 服务与测试；验收作者状态往返、外来引用拒绝、旧备份兼容。依赖：无。
2. 审核与重试：修改 quality/auto-director 服务及路由；验收无效审核不放行、实际尝试耗尽预算、完整阶段回归。依赖：无。
3. 创作入口：修改首页/卷导演；验收默认作者流程、有效预算、模型保存和正确卷运行。依赖：无。
4. 改稿与导出：修改 Writer 服务、章节页、新增成稿路由与测试；验收版本保留、审核失效、并发冲突、封卷保护与准确导出。依赖：无。
5. 集成验收与启动：完成完整测试和浏览器 QA，更新使用文档，检查实际服务就绪。依赖：1–4。

交付前核对：从新书入口到修改、审核、导出和恢复的控制与状态一致，实际作品库不用于测试。

## QA inventory

- 首页：统一向导入口、默认作者模式、模式切换、长度/预算、模型测试状态、验证失败与重试。
- 当前卷：封存第一卷/活跃第二卷时选择第二卷，运行按卷匹配，作者仍能切换卷。
- 章节：打开正文、进入编辑、取消、保存、刷新保留内容、版本切换、对比历史稿、另存恢复、旧报告失效、过期保存拒绝。
- 导出：TXT/Markdown、单卷/全书、批准稿筛选、无正文错误、不包含失败候选。
- 视觉：桌面和窄屏检查向导、章节正文/编辑状态；截图拼图缩略图检查，禁止原图回喂。

## 开源学习与本次创新

- 公共项目在 `research/opensource-downloads/` 浅克隆且全局忽略；固定 commit、来源、许可证和文件规模存于 `research/opensource/sources.json`。
- 仅将原创架构笔记和本地需求验证放入研究库，不把 GPL/AGPL 项目源码/资产/文案/提示词集成进生产。
- 本轮新增正文历史逐行对比及“另存恢复为新稿”：保持父子血缘、每个原版本不可变、作者可预审恢复内容并编辑后保存；恢复新稿重新质量审查。
- diff 比较是有界 LCS，超过18万行对比单元展示有界摘要，保护长章浏览器内存。

## Assumptions to review

1. 本机单用户（HIGH）：本轮不改变部署边界。
2. 人工修改先成为工作稿（HIGH）：作者可继续修改，正式批准需要新审核。
3. TXT/Markdown 首批导出（LOW）：Word/PDF 留作后续需求。


## Continued workflow hardening

This pass also corrected long-running workflow details:

- Home live status is bound to the newest active run and its actual project/run destination.
- Chapter and draft selection cancel or discard stale responses; errors from older requests cannot replace the selected chapter.
- Automatic director chapter details are independently guarded, and the most recent committed draft is selected by its chapter ID.
- Pausing a volume pauses its owned worker run. Continue/extend share the retry checkpoint limit, preventing volume controls from bypassing exhausted retries.
- Unsealing and resealing creates a new immutable snapshot version. Existing versioned snapshots and source-bound windows remain recoverable; dependent next-volume planning windows are invalidated when their source snapshot changes.
- Snapshot reads explicitly choose the volume sealed reference, or latest version when the volume is open.

Validation for this pass: the whole API suite uses isolated SQLite in-memory databases and fake providers; frontend race scenarios use delayed responses against browser-stubbed project data.
