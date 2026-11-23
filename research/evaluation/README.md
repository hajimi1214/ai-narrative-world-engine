# 中文长篇文学评测基线

本目录保存第四阶段的评测协议和原创合成任务清单。它不包含真实小说正文，也不把现有 `golden_novel_fixture.py` 当作文学质量样本。

## 状态

- `pilot-manifest.json` 是冻结的任务设计，状态为 `PLANNED_SYNTHETIC_PILOT`。
- 目前没有真实模型输出、人工读者评分或作者修改记录。
- `reader-rating-template.json` 是去除模型信息后的盲评记录格式。
- `experiment-record-template.json` 固定三种对照条件和成本记录字段。
- 评分汇总代码位于 [literary_evaluation.py](../../apps/api/app/literary_evaluation.py)，只做离线分析，不参与质量门或正文采用。
- 汇总会单独输出人类评分、模型 Judge 与人类的逐维偏差、工程指标以及确认 token、未知调用、预算预留和修改时间。缺少人类评分时不会生成文学胜负结论。

从仓库根目录分析一份冻结记录：

```powershell
.\.venv\Scripts\python.exe apps/api/scripts/analyze_literary_experiment.py research/evaluation/experiment-record-template.json
```

## 对照条件

- `A_BASELINE` 使用相同作者任务和基础 Writer，只有短章节任务和显式上下文。
- `B_MEMORY` 在同一 Writer 上增加角色记忆和已采用章节摘要，保留相同模型、预算和章节顺序。
- `C_ENGINE` 使用完整世界引擎，包括正式事实、角色认知、章节分支、Critic 证据和采用事务。

每个条件必须使用相同任务、目标章节数、字数上限、模型版本、温度、随机种子、失败重试规则和预算。随机化只改变评审看到的文本顺序和匿名编号。

## 当前可报告的结果

只有工程回归与评测代码自检已经完成。没有人类评分，因此不能报告任何条件在文学质量或阅读意愿上的胜负。
