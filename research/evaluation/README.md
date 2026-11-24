# 中文长篇文学评测协议 v2

本目录冻结第四阶段建立的原创试点任务和第五阶段/第六阶段使用的评审、实验记录格式。当前没有真实模型输出、读者评分、Critic 判断或作者修订数据。样本任务是原创合成设计，不是已完成小说。

## 当前证据状态

- `pilot-manifest.json` 为设计清单，状态 `PLANNED_SYNTHETIC_PILOT`，四个任务覆盖悬疑、现实情感、历史奇幻和科幻成长。
- `experiment-record-template.json` 记录三种条件、模型与 prompt 指纹、采样参数、预算、全部任务/条件运行结果、盲评、工程指标和成本代理。目前为 `PLANNED`，尚未冻结模型实验。
- 文学结果状态应为 `NOT_EVALUATED`。不得从程序测试、人物卡字段、Writer 自报、Critic 分数或成功生成率推出真实读者偏好。

## 文本评分量表

完整定义在 [literary-evaluation-rubric.md](../../docs/diagnostics/2026-11/literary-evaluation-rubric.md)。量表 v2 共有 21 个分项，覆盖人物可信度、成长、动机、主动性、关系变化、声音差异、剧情吸引、因果、冲突、悬念、推进、转折、伏笔、情绪、节奏、结构、中文自然度、描写、对白、连续性和继续阅读意愿。

评审单位要显式记录为单章或三章连续窗口。只看单章时，如果无法观察人物成长或伏笔回收，可以在 `scores` 中填 `null`，并在 `score_abstentions` 给出原因。不能用猜分填满量表。每个可评分维度需要一条正文证据和整份评审的信心值。

读者端模板 [reader-rating-template.json](reader-rating-template.json) 不包含条件、作品任务 ID、模型或 prompt。研究者揭盲后的数据使用 [unblinded-rating-record-template.json](unblinded-rating-record-template.json)。评分模式为 `blind-reader-rating-v2`。

## 三种比较条件

实验记录固定 `A_BASELINE`、`B_MEMORY` 和 `C_ENGINE`。每个条件必须使用相同作者任务、章节窗口、目标字数、模型版本、temperature、top-p、seed、失败重试规则和每条件 token 预算。

- A 使用任务单和基础 Writer，不提供结构化角色记忆或世界状态链。
- B 使用与 A 相同的 Writer，在同一已采用上下文上增加角色记忆和前文摘要。
- C 使用完整小说引擎，包括角色认知、正式世界状态、候选章节分支、Critic 正文证据和采用事务。

操作者需要逐项记录 A/B/C 实际关闭或启用的功能、配置和 prompt 指纹。若实现不能让 A/B 条件复用同一基础 Writer 或不能冻结同一供应商模型，需暂停比较并记录设计偏差。不得把不同模型或不同预算造成的差异归结为世界引擎效果。

冻结前确认 `generation` 字段全部完整、预算为有限正数，所有 task × condition 组合都记录为 `COMPLETED`、`FAILED`、`BLOCKED`、`INTERRUPTED` 或 `NOT_STARTED`。失败与缺失结果保留在分析分母与输出状态汇总中，不能静默丢弃。

## 可复现工具

Python 评测模块是离线工具，不在质量门或正文采用流程中运行。

验证冻结实验记录

```powershell
uv run --locked python apps/api/scripts/validate_literary_experiment.py research/evaluation/my-frozen-experiment.json
```

从状态为 `FROZEN` 的实验记录生成匿名评审包和单独的研究者揭盲表

```powershell
uv run --locked python apps/api/scripts/prepare_literary_blind_review.py research/evaluation/my-frozen-experiment.json output/literary-blind-review --raters reader-a,reader-b --seed 20261123
```

输出目录的父目录必须存在，目标目录必须尚不存在。源记录的每个完成正文需填仓库内相对 `text_path` 和其 SHA-256。脚本校验三条件任务网格、模型/prompt 指纹、预算、失败记录、正文路径和哈希后，在 `reader-packets/` 写只含匿名文本 ID、正文和评审 ID 的 JSON。`researcher-only/` 内保存条件映射和盲包顺序，绝不可发给评审。每位评审的呈现顺序按固定 seed 独立打乱。脚本不会生成模型文本、读取仓库外小说、连接模型或写入数据库。

生成后把 `researcher-only/preparation-summary.json` 的 `seed` 和 `experiment_fingerprint` 作为 `blind_preparation` 存入研究者记录。评审结束后将盲评原始 JSON 放入 `blind_ratings`，将研究者专用的映射和盲包分配表分别放入 `unblinding_map`、`blind_assignments`，然后分析。分析会按原 seed 和冻结正文网格重算盲包分配，验证映射与条件指纹一致。评审记录有改动时需保留冻结文本与原准备记录，不能重新随机化后覆盖。报告会显示每位评审未完成项目的数量，并保留该分母。

输出目标创建时采用独占目录语义，已存在的目录会报错且保留原内容。评测统计中的 `scheduled_count` 表示计划运行单元，`attempted_count` 仅统计尝试次数大于零的单元。未启动、阻断和中断记录仍保留在网格与分母中。

```powershell
uv run --locked python apps/api/scripts/analyze_literary_experiment.py research/evaluation/my-rated-experiment.json
```

分析报告逐维展示 task 等权条件均值和 task-cluster 标准误、所有条件对的人工评审配对差异、以 `task_id` 为独立单位的 bootstrap 区间、多人重叠样本的加权 pairwise Cohen's kappa 及 task-cluster 区间、AI Judge 与人类 task 均值的误差/偏差、逐位评审缺失、下一章意愿、AI 感知、修订负担和生成已排期/实际尝试/完成/失败数。配对差异先在每个 task 内汇总再对 task 等权平均。少于四个独立任务不计算配对效果区间，返回 `INSUFFICIENT_TASKS`。少于五个共享项目不计算评审一致性区间，显示 `LOW_SAMPLE_DESCRIPTIVE`。四任务 pilot 的区间仍属探索性描述。pairwise 加权 kappa 是描述性一致性，不等同于验证模型或量表有效性。

## 盲评运行规范

1. 在评审开始前冻结记录、模型、prompt、采样参数、A/B/C 配置、预算、失败策略和所有已生成正文的 SHA-256。
2. 给至少两位中文母语读者独立分配盲包。评审之间不沟通，不向其提供系统状态、条件、模型名、Critic 报告或成本数据。
3. 评审提交每维评分、具体正文证据、信心、下一章意愿、AI 感知和修订分钟数。证据使用段落位置或简短必要原文，不收集与评分无关的个人信息。
4. 保留未完成项和弃评理由，不用平均值抹掉分歧。参与者应知晓评测用途、资料保存方式和退出方式。
5. 收回评分后才把研究者揭盲表与盲评数据合并。原始姓名/联系方式与作品文本分开保存，公开报告只使用聚合数据。
6. 若文本没有发布授权，仅由获准评审访问，不在公开报告附全文或长段落引用。使用真人作品前需先确认作者许可范围。

## 成本与工程数据

`engineering_metrics` 与 `cost_and_editing` 都分开于文学分数。至少记录运行状态、每条件尝试数、章节窗口、确认 token、未知 usage 调用、预算 reserve、耗时、失败/重试次数和作者改稿分钟数。供应商账单字段在接入真实对账前保持 `UNKNOWN`。每千字确认 token 是用量代理，不是美元成本。

## 统计与结论纪律

四个原创任务只构成试点设计，不足以支持普遍优劣或文类结论。确认性结论要增加独立任务、多个模型，或事先做功效分析。主要推断的重抽样单位是原创 task，而不是同一小说中高度相关的章节或同一读者的重复评分。当前自动汇总提供任务聚类配对 bootstrap 和基础多人一致性描述，但不做因果识别、功效计算或跨模型统计。

报告必须区分

- **工程正确性**由自动测试和状态轨迹证明。
- **事实连续性**由来源、时间线、视角可见性和事件证据检查。
- **AI Judge**是单独的辅助评估，不是真实读者。
- **真实读者体验**只由匿名读者反馈支持。
- **作者采用与修订负担**单独记录，不能由读者评分替代。

任何一层没有数据时都报告 `NOT_EVALUATED`、`NOT_REPORTED`、`UNKNOWN` 或明确缺失数。现有 Pilot manifest 和记录模板没有结果，不应汇报 A/B/C 的优劣。
