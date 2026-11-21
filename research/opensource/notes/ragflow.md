# RAGFlow

来源：[`sources.json#ragflow`](../sources.json)，commit `91c702e86be3dcb3cabc9f6d81e485d02241bef8`，README 标示 release 1.0.0，许可证 Apache-2.0。此次不安装、运行其服务或使用其索引数据库。

## 流程与可追溯性观察

- 官方 [`docs/guides/knowledge_compilation/overview.md`](../../opensource-downloads/ragflow/docs/guides/knowledge_compilation/overview.md) 将文档摄取到结构化知识描述为“文件→parser→chunker→compiler→indexer”流水线。
- 一个文档处理失败不应模糊为整个数据集成功；处理状态及来源应可单独核验和重试。
- 知识编译可以输出不同视图（graph、tree、page index、mindmap、timeline、wiki），这些是来自源文档的**辅助 artifact**而非替代原文。
- 官方使用案例要求抽取结果与原文逐项核对、来源引用能回查；变更模板后需要显式重跑，模板保存本身不会重建旧产物。

## 本项目学习成果

- 连续性报告应列出它使用的章节/scene/fact 证据和 fingerprint，而非只输出“发现矛盾”。
- 将质量审核、修复、人物记忆投影和向量 index 视作不同可重试阶段，避免某个检索组件失败就丢掉已保存草稿。
- 先对 1–2 个项目/章节运行可复现基准，再决定是否重建/替换索引，绝不在背景中把旧用户事实当成新抽取结果覆盖掉。

## 不照搬

RAGFlow 面向通用/企业文档与多服务部署。它的存储后端、部署形态和连接器不适合作为本地单作者应用的依赖；只学习来源管理、阶段状态及知识 artifact 的边界。
