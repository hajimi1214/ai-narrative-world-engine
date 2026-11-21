# Pagekin / 书童

来源：[`sources.json#pagekin`](../sources.json)，公开 commit `d786159d1371bfd38938e1a7c74422458b3247ff`，README 标注产品版本 1.5.0，项目声明 GPL-3.0。本次只阅读公开 README 及少量对应类型/流程模块，没有运行桌面 app 或安装其依赖。

## 作者流程与数据观察

- README 将项目组织为作者确认的架构、角色、章节蓝图、正文草稿、审稿、修稿和定稿步骤；重要状态要由作者确认。
- `src/shared/chapter-reopen.ts`（该固定 commit）把已定稿章节重新打开表达为一个新的 working draft；请求携带 chapter number / optional initial draft ID，响应区分是否已存在 open draft，并定义下游章节/人物影响摘要类型。
- `electron/controllers/chapter-lifecycle-controller.ts` 把章节重新打开和下游影响预览分开，由作者确认后才变成新工作稿；重复打开返回同一份尚未定稿的稿件。
- `src/shared/blueprint-continuity-suggest.ts` 只接受白名单规划字段，并把“原文字(before)”与“建议改后文字(after)”表达为待接受/拒绝的 proposal，不让模型直接覆盖作者稿。
- `src/shared/author-manuscript-import.ts` 把章节导入预览分为 ready、exact duplicate、conflict，并返回新增/重复/冲突的章号。
- `electron/repositories/revision-repository.ts::create` 在事务里先校验 expected source，再分配修订序号并保存内容和父稿来源；原稿并发变化会使保存失败而不是覆盖新稿。
- `electron/repositories/review-cycle-repository.ts` 为审稿、作者确认、修稿和再审串联有来源证明的状态；旧审稿不能自动授权改过的正文。
- README 说明人物状态、关系、到期伏笔按目标章上下文加入下一章；参考书拆解与原稿续写用途隔离；导入区分新章、完全重复与内容冲突，并可在已提交批次后续导入。
- 复核其公开 TypeScript 合同也能看到：作者修改以 source revision 标识并捕获冲突，连续性检查结果绑定 revision，章节导入有明确 disposition。

## 本项目学习成果

- 用户可先看 diff，再把历史文本**另存为新的作者稿**；所有原版本不被修改，乐观基线校验、封卷校验和 fresh review 仍按本项目规则执行。
- 把“模型找问题→作者接受部分建议→提交为新版本”的模式迁移到章节改写 UI，而不是直接改正式章节内容。
- 为整章重开做下游影响分析时，把重新审核质量报告、生成新的下游计划和因果重放分成不同确认步骤；不能以一条操作暗中重算正式世界历史。
- 原稿/参考稿需要带来源标记；默认不可将参考资料当成作者自写正式正文。

## 主要差异

Pagekin README 中大量作者可见流程可作为交互参考；本项目仍须使用自己的 scene commits、知识限制、因果历史、不可变封卷 snapshot 和 quality audit。不能用普通版本复制替代 formal-state replay。
