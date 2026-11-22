# 长篇创作技术与文学评测研究

## 研究边界

本研究优先引用论文原文仓储、同行评审论文元数据、官方项目文档和固定 commit 的开源记录。论文结论只按原任务域解释，不把问答、代理模拟或短篇故事结果外推成中文长篇文学质量证据。以下“工程含义”是对本项目的设计推论，不是论文原作者的产品建议。

## 关键技术结论

### 长上下文不能代替检索和状态管理

Liu 等人通过合成键值检索与多文档问答任务测量长上下文模型，发现相关信息位于输入中段时，多个被测模型可能不如信息位于开头或结尾时稳定。它说明把更多前文拼进 prompt 不能保证模型稳定使用所有事实。论文没有测试整本小说的因果连续性，也没有比较本项目检索器。[Lost in the Middle，TACL 2024](https://aclanthology.org/2024.tacl-1.9/)

### RAG 的价值包含可更新知识和来源，不只在召回

原始 RAG 工作将检索模块与生成模型组合，针对知识密集任务和知识更新/来源给出研究动机及实验。该论文验证的是其 NLP benchmark 任务，不证明检索引用会自动转化成可靠叙事。对本项目更相关的是保持来源 revision、chunk、权限与 authority tier，而不是把召回文本无差别当成正式事实。[Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks，NeurIPS 2020 / arXiv](https://arxiv.org/abs/2005.11401)

### 长期记忆需要检索、压缩、更新和时间边界

MemGPT 提出让 LLM 显式管理有限上下文与外部存储之间的数据移动。Generative Agents 将观察、记忆检索、反思与计划用于小型交互社会模拟。它们证明这些架构可在相应演示任务中工作，不证明有了“长期记忆”就会得到稳定人物弧线或高质量长篇。书稿系统仍需评测遗忘、错误记忆、人物知识泄漏、过去状态被新事实覆盖等失败模式。[MemGPT，arXiv 2023](https://arxiv.org/abs/2310.08560) · [Generative Agents，UIST 2023 / arXiv](https://arxiv.org/abs/2304.03442)

### 规划与分段生成有依据，但长篇纵向效果未由短任务证明

Plan-and-Write 将故事计划与段落生成分开，并在论文设定的故事生成任务上报告效果。它支持明确中间计划作为可审查接口，但不是多卷长篇读者研究。LongWriter 通过长输出样本构造与训练流程研究 10,000 字级输出，强调输入上下文很长不意味着模型自然具备长输出能力。对本项目而言，规划格式、分章窗口和续写预算都是能力边界，需要以目标模型、中文、实际长度复测。[Plan-and-Write，AAAI 2019](https://doi.org/10.1609/aaai.v33i01.33017378) · [LongWriter，arXiv 2024](https://arxiv.org/abs/2408.07055)

### 角色对话基准不能代表角色驱动的小说

CharacterEval 是中文角色扮演对话基准，关注角色扮演对话质量。它可以启发角色设定遵守、身份一致和互动性测项，但其对话轮次分数不能代表 30 章以上的动机变化、关系演变、视角稳定、后果承接或情感弧线。[CharacterEval，ACL 2024](https://doi.org/10.18653/v1/2024.acl-long.638)

### 中文长文本与通用写作基准可作组件，不是长篇替身

LOT 是 story-centric 的中文长文本理解/生成基准，原论文描述其故事样本为数百字，并测长距离篇章与常识关系、连贯性和可控生成。它比通用短文本更接近中文故事生成，但仍远短于多章或多卷连载。[LOT，arXiv 2021](https://arxiv.org/abs/2108.12960)

WritingBench 覆盖六个写作域和一百个子域，并提出 query-dependent criteria 与 Critic 评分。它可提供任务覆盖面和 rubric 设计参考。其摘要报告的模型排序来自该 benchmark 和其评估框架，不代表读者接受度，也不能据此断言 Critic 与人类专家等价；需要在本项目目标域独立复验。[WritingBench，arXiv 2025](https://arxiv.org/abs/2503.05244)

## Critic 与自动评测边界

G-Eval 研究 LLM 作为 NLG 评估器，并在特定任务数据上比较与人工判断的一致性。Judge 模型仍可能有提示敏感性和推理局限。MT-Bench / Chatbot Arena 相关研究讨论位置、冗长和自我偏好等评审偏差。FActScore 把长文本拆成原子事实，再按外部知识源支持率评估 factual precision。它适合事实主张核验，不评价人物魅力、潜台词或阅读愉悦度。[G-Eval，arXiv 2023](https://arxiv.org/abs/2303.16634) · [Judging LLM-as-a-Judge，arXiv 2023](https://arxiv.org/abs/2306.05685) · [FActScore，arXiv 2023](https://arxiv.org/abs/2305.14251)

Busselle 与 Bilandzic 的 Narrative Engagement 工作提供对叙事投入的受众研究视角，适合作为读者实验设计参考。它不是 AI 小说打分器，也不能直接用单一总分代表全部文学维度。[Measuring Narrative Engagement，2009](https://doi.org/10.1080/15213260903287259)

## 中文长篇质量框架

以下是建议在本项目用来组织评测的作者与读者维度，不宣称存在一套已被论文证明适用于所有中文长篇的统一量表。

| 维度 | 要观察的文本行为 | 可操作的检查方式 | 不能由什么单独代表 |
|---|---|---|---|
| 人物可信度 | 欲望、认知、能力、风险和选择相互约束 | 盲读者指出行为动机与前文依据，记录不可信处 | 人物卡字段齐全、角色自报目标 |
| 人物成长 | 新选择来自累积经验并改变后续行为 | 跨章节前后对照具体决策与付出的代价 | 人物状态 JSON 发生变化 |
| 因果逻辑 | 结果能追溯到行动，后果能进入后续场景 | 编辑画事件链并标出无因之果/有因无果 | 时间戳单调、场景 commit 完整 |
| 冲突张力 | 目标互斥、代价真实、选择影响局势 | 读者标记冲突升级、选择点及其后果 | 剧情任务单包含 conflict 字段 |
| 情感感染力 | 情感由具体关系、行动与损失引发 | 盲读后回忆关键情绪点和触发段落 | Critic 的单项分数 |
| 悬念与阅读吸引力 | 未知信息和承诺推动下一章阅读 | 下一章阅读意愿、弃读点、悬念回收评价 | 伏笔数量、章节 cliffhanger 标签 |
| 节奏 | 叙述密度、场景推进、转折与休整适配 | 多章节节奏曲线与读者停顿/跳读反馈 | 字数、对话比、景物占比单项规则 |
| 文风 | 叙述声音稳定但具有语义和句法变化 | 作者/编辑盲评，比较本人认可样本 | 禁用词频或“反 AI”词表通过 |
| 伏笔 | 首次呈现清楚、后续有因果回响、回收不牵强 | 读者预测、追溯伏笔与回收文本 | 数据库 `PAID_OFF` 状态 |
| 中文表达 | 语域、指代、成语、对白声线、节奏自然 | 母语编辑标错并给出自然度和修订意见 | 通用英文 NLG metric 或字数规则 |

## 开源产品观察

既有研究记录将上游版本和许可证固定在 `research/opensource/sources.json`。这里引用的是产品/架构取舍，不把仓库 README 或功能存在当作效果研究。[Pagekin / 书童，GPL-3.0](https://github.com/EthanYoQ/Pagekin/tree/d786159d1371bfd38938e1a7c74422458b3247ff) · [novelWriter，GPL-3.0-or-later 与多许可证依赖](https://github.com/vkbo/novelWriter/tree/dc483f9c881dadbc847b90ccf8ecf67658dc30e2) · [BookStack，MIT](https://github.com/BookStackApp/BookStack/tree/34edd552294e4ba0603e55542ae5fafef28b96e7) · [RAGFlow，Apache-2.0](https://github.com/infiniflow/ragflow/tree/91c702e86be3dcb3cabc9f6d81e485d02241bef8)

- Pagekin 的章节草稿、审稿、逐条确认和修订流程，可启发把作者判断留在可见节点。其公开交互本身不是写作质量比较证据。[固定 commit 设计笔记](../../../research/opensource/notes/pagekin.md)
- novelWriter 的纯文本作品、显式资料分类和可重建索引，说明可读文档与结构化索引能分层。纯文本不自动解决因果历史与事务采用。[固定 commit 设计笔记](../../../research/opensource/notes/novelwriter.md)
- BookStack 的不可变修订和“恢复为当前新版本”有助于理解作者稿版本语义。页面历史不等于小说事实重放。[固定 commit 设计笔记](../../../research/opensource/notes/bookstack.md)
- RAGFlow 的 parse/chunk/index 阶段、任务状态与来源 provenance，适合借鉴可独立重试的数据管线。其服务规模与部署方式不直接适合本地单作者引擎。[固定 commit 设计笔记](../../../research/opensource/notes/ragflow.md)

## 对本项目的研究转化

先用**短期验证**解决输入/预算/密钥/视角这类工程正确性。随后以许可清晰或原创中文样本建立人工标注，最后才讨论模型训练、检索重排或 Agent 数量。研究来源和关键结论更新时保留原链接、日期、任务范围与本项目实测，不用模型生成的文献摘要替代原文核对。
